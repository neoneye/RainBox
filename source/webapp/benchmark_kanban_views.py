"""The /benchmark_kanban page — the kanban "first slice" decision matrix
(notes/kanban-design.md roadmap item 1) on its own page, so the general
/benchmark suite stays fast and the 2×2 comparison (board context format ×
invocation mechanism) reads as one experiment.

Reuses the shared benchmark-suite page + BenchmarkRunner machinery from
benchmark_views/benchmarks.runner; only the spec set, runner instance, and
endpoints differ.
"""

import json

from flask import Response, abort, request

import db

from benchmarks.runner import KANBAN_BENCHMARK_SPECS

from .benchmark_views import render_benchmark_page
from .core import app, kanban_benchmark_runner

KANBAN_BENCHMARK_DESCRIPTIONS: dict[str, str] = {
    "kanban_md_struct": (
        "Board serialized as MARKDOWN + one instruction (move/claim/complete/"
        "fail/note); the model answers with one structured KanbanOpResponse. "
        "Correct iff the operation and every uuid match exactly."
    ),
    "kanban_json_struct": (
        "Same instructions, board serialized as JSON; structured output."
    ),
    "kanban_md_tools": (
        "Board as MARKDOWN; the model must make exactly ONE function call "
        "(move_task/claim_task/complete_task/append_event) with the right "
        "uuids. Requires a function-calling target."
    ),
    "kanban_json_tools": (
        "Same, board serialized as JSON; function calling."
    ),
}

KANBAN_INTRO = (
    "The kanban first-slice decision matrix: board context format (markdown "
    "vs JSON) × invocation mechanism (structured output vs function calling). "
    "Each trial is a synthetic board serialized with the production renderers "
    "plus one operation instruction; correct iff the op and the uuids match "
    "exactly. The winning cell picks the DEFAULTS for the first LLM kanban "
    "worker (notes/kanban-design.md). Tool trials are capped at 60s; after 2 "
    "timeouts the benchmark is abandoned and marked failed."
)


@app.route("/benchmark_kanban")
def benchmark_kanban_page() -> str:
    return render_benchmark_page(
        "Benchmark kanban", KANBAN_INTRO,
        KANBAN_BENCHMARK_SPECS, KANBAN_BENCHMARK_DESCRIPTIONS,
        "benchmark_kanban_state", "benchmark_kanban_start", "benchmark_kanban_stop",
        history_endpoint="benchmark_kanban_history",
    )


@app.route("/benchmark_kanban/state")
def benchmark_kanban_state() -> Response:
    kanban_benchmark_runner.ensure_targets_populated()
    return app.response_class(
        json.dumps(kanban_benchmark_runner.get_state()),
        mimetype="application/json",
    )


@app.route("/benchmark_kanban/history")
def benchmark_kanban_history() -> Response:
    """Stored per-cell results for this suite; kept off the once-a-second
    /state poll (see benchmark_basic_history)."""
    return app.response_class(
        json.dumps(db.benchmark_history("kanban")),
        mimetype="application/json",
    )


@app.route("/benchmark_kanban/start", methods=["POST"])
def benchmark_kanban_start() -> Response:
    target_uuid = request.args.get("target_uuid") or request.form.get("target_uuid")
    target_uuids = [target_uuid] if target_uuid else None
    # `bench` selects one cell; absent runs the whole row.
    raw_bench = request.args.get("bench") or request.form.get("bench")
    bench_indices = None
    if raw_bench not in (None, ""):
        try:
            bench_indices = [int(raw_bench)]
        except ValueError:
            abort(400, "bench must be an integer")
    try:
        started = kanban_benchmark_runner.start(
            app, target_uuids=target_uuids, bench_indices=bench_indices
        )
    except ValueError as e:
        abort(400, str(e))
    return app.response_class(
        json.dumps({"started": started}),
        mimetype="application/json",
    )


@app.route("/benchmark_kanban/stop", methods=["POST"])
def benchmark_kanban_stop() -> Response:
    kanban_benchmark_runner.stop()
    return app.response_class(
        json.dumps({"stopping": True}),
        mimetype="application/json",
    )
