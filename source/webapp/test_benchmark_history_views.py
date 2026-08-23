"""The /history endpoints — and the guarantee that history stays off /state."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

import db
from db.models import BenchmarkResult
from db.models import db as _db
from webapp.core import app

TARGET = uuid4()


@pytest.fixture(autouse=True)
def clean_rows():
    with app.app_context():
        _db.session.query(BenchmarkResult).delete()
        _db.session.commit()
    yield
    with app.app_context():
        _db.session.query(BenchmarkResult).delete()
        _db.session.commit()


def _seed(spec_set="general", benchmark_name="base64_decode"):
    with app.app_context():
        db.record_benchmark_result(
            spec_set=spec_set, benchmark_name=benchmark_name, target_uuid=TARGET,
            target_label="t0.15 c8k struct", model_name="gemma4:e4b",
            provider="ollama", status="done", trials_done=5, trials_total=5,
            correct=5, mistakes=0, failures=0, total_elapsed=9.5,
            reasoning_chars=None, content_chars=None, error=None,
            config_fingerprint="cfg", spec_fingerprint="spec",
            started_at=datetime.now(UTC), ended_at=datetime.now(UTC),
        )


@pytest.mark.parametrize("page,spec_set,name", [
    ("benchmark_basic", "general", "base64_decode"),
    ("benchmark_kanban", "kanban", "kanban_md_struct"),
    ("benchmark_story", "story", "story_text"),
])
def test_history_endpoint_returns_the_stored_cell(page, spec_set, name):
    _seed(spec_set=spec_set, benchmark_name=name)
    resp = app.test_client().get(f"/{page}/history")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body[name][str(TARGET)]["complete"][0]["correct"] == 5


def test_history_endpoint_is_scoped_to_its_own_page():
    """The three suites share one table; /benchmark_basic must not serve the
    kanban page's rows."""
    _seed(spec_set="kanban", benchmark_name="kanban_md_struct")

    assert app.test_client().get("/benchmark_basic/history").get_json() == {}


def test_history_endpoint_is_empty_with_no_rows():
    assert app.test_client().get("/benchmark_basic/history").get_json() == {}


def test_state_does_not_carry_history():
    """/state is polled once a second. History on it would put every stored
    result on the wire every second for as long as the page is open — the same
    reason story artifacts are kept off it."""
    _seed()
    body = app.test_client().get("/benchmark_basic/state").get_json()

    assert "history" not in body
    assert "complete" not in str(body)


@pytest.mark.parametrize("page", ["benchmark_basic", "benchmark_kanban",
                                  "benchmark_story"])
def test_page_wires_up_history(page):
    body = app.test_client().get(f"/{page}").get_data(as_text=True)

    # Fetched from its own endpoint, not read off the polled state.
    assert f"/{page}/history" in body
    assert "function loadHistory" in body
    # Merged into cells that have no live result.
    assert "function historicEntry" in body
    # Refetched when a run ends, so a finished cell's card is current.
    assert "wasRunning" in body


@pytest.mark.parametrize("page", ["benchmark_basic", "benchmark_kanban",
                                  "benchmark_story"])
def test_page_has_the_hover_card_styles(page):
    body = app.test_client().get(f"/{page}").get_data(as_text=True)

    assert "cell-history" in body
    assert "td.bench:hover .cell-history" in body
    assert "historic" in body
