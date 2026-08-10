"""The worker honours a cell selection, and keeps the column index honest."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run_worker(request: dict) -> list[dict]:
    """Drive benchmarks.worker as the runner does, with a target uuid that
    resolves to nothing — enough to see which cells it attempts."""
    proc = subprocess.run(
        [sys.executable, "-m", "benchmarks.worker"],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin",
             "DATABASE_URL": "postgresql+psycopg://localhost/rainbox_claude"},
        timeout=180,
    )
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def attempted(events: list[dict]) -> list[int]:
    return sorted({e["bi"] for e in events
                   if e.get("t") == "bench_status" and e.get("status") == "running"})


def test_only_the_chosen_cell_runs():
    events = run_worker({
        "target_uuid": "00000000-0000-0000-0000-000000000000",
        "skip_warmup": True, "spec_set": "story", "bench_indices": [2],
    })
    assert attempted(events) == [2]


def test_the_emitted_index_is_the_column_not_the_position_in_the_subset():
    """If the worker renumbered the subset, cell 2's result would land in
    column 0 and the page would lie about which benchmark ran."""
    events = run_worker({
        "target_uuid": "00000000-0000-0000-0000-000000000000",
        "skip_warmup": True, "spec_set": "story", "bench_indices": [1, 3],
    })
    assert attempted(events) == [1, 3]


def test_no_selection_runs_the_whole_row():
    events = run_worker({
        "target_uuid": "00000000-0000-0000-0000-000000000000",
        "skip_warmup": True, "spec_set": "story", "bench_indices": None,
    })
    assert attempted(events) == [0, 1, 2, 3]
