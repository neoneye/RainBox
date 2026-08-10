"""The /benchmark_story page — ten-turn conversation benchmarks.

Reuses the shared benchmark-suite page + BenchmarkRunner machinery from
benchmark_views/benchmarks.runner; only the spec set, runner instance, and
endpoints differ. The one addition is `show_artifacts`, which turns on the
per-trial Copy buttons — these trials produce a story, and a story is worth
reading.
"""

import json

from flask import Response, abort, request

from benchmarks.runner import STORY_BENCHMARK_SPECS

from .benchmark_views import render_benchmark_page
from .core import app, story_benchmark_runner

STORY_BENCHMARK_DESCRIPTIONS: dict[str, str] = {
    "story_text": (
        "Five turns of plain text. Each turn asks for the next ~200-word "
        "section, resending the whole conversation. Correct iff all five "
        "sections arrive within 100–350 words."
    ),
    "story_struct": (
        "Same five turns, structured output: `section_text` (the piece) plus "
        "`section_reviewer`, a brutally harsh critique of that same section. "
        "Correct iff every section is in range and every critique is present."
    ),
    "story_text_tool": (
        "Same five turns, plus one tool. Before each section the model must "
        "call `random_number` exactly once; the integer it returns must then "
        "appear literally in the text. Correct iff every section called the "
        "tool once and contains its number — which a model that calls the "
        "tool and ignores the answer cannot fake. Requires a function-calling "
        "target. Copy a trial to see, per section, whether the tool ran, how "
        "often, what it returned, and whether the model used it."
    ),
    "story_struct_tool": (
        "The crossover: structured output AND function calling on every one "
        "of the five turns. The hardest of the four for a local model."
    ),
}

STORY_INTRO = (
    "Five-turn conversations that write a short piece a section at a time, "
    "across the text/structured × no-tools/tools matrix. Every turn resends "
    "the system prompt and the whole history and appends one message, so each "
    "prompt is a strict prefix extension of the last — the shape a KV cache "
    "is built for. That makes this suite both a capability test and the "
    "workload that puts a real number on the /activity dashboard: check the "
    "reusable-prefix rate there after a run. "
    "Each trial draws a different brief from a list of 100 — an AI "
    "politician's stump speech, a layoff memo, a Black Mirror pitch, a recipe "
    "that turns personal — so one sweep leaves a dozen unrelated pieces "
    "rather than a dozen variations on one theme. "
    "Three trials each, so budget roughly 10–20 minutes per target for the "
    "full set. Hover a Copy button to see which brief it holds; click to put "
    "the piece on the clipboard, or take the json for the full exchange: "
    "system prompt, every request and response, and what the tool did on "
    "each turn. Every section heading carries its own verdict, so a failed "
    "trial can be read rather than re-run."
)


@app.route("/benchmark_story")
def benchmark_story_page() -> str:
    return render_benchmark_page(
        "Benchmark story", STORY_INTRO,
        STORY_BENCHMARK_SPECS, STORY_BENCHMARK_DESCRIPTIONS,
        "benchmark_story_state", "benchmark_story_start", "benchmark_story_stop",
        show_artifacts=True,
        artifact_endpoint="benchmark_story_artifact",
    )


@app.route("/benchmark_story/artifact")
def benchmark_story_artifact() -> Response:
    """One trial's piece, as markdown to read or JSON to troubleshoot.

    Served on request rather than carried in the polled state: a sweep's
    transcripts run to hundreds of kilobytes, and the page refreshes about
    once a second.

    The JSON is sent as an attachment so it lands as a file — the point is to
    open it in an editor next to the code, not to squint at it in a tab.
    """
    try:
        target = int(request.args.get("target", ""))
        bench = int(request.args.get("bench", ""))
        trial = int(request.args.get("trial", ""))
    except ValueError:
        abort(400, "target, bench and trial must be integers")

    artifact = story_benchmark_runner.get_artifact(target, bench, trial)
    if artifact is None:
        abort(404, "no artifact recorded for that trial")

    if request.args.get("format") == "json":
        name = f"story-{bench}-{target}-trial{trial + 1}.json"
        return app.response_class(
            json.dumps(artifact["transcript"], indent=2, ensure_ascii=False),
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )
    return app.response_class(
        artifact["markdown"] or "", mimetype="text/plain; charset=utf-8"
    )


@app.route("/benchmark_story/state")
def benchmark_story_state() -> Response:
    story_benchmark_runner.ensure_targets_populated()
    return app.response_class(
        json.dumps(story_benchmark_runner.get_state()),
        mimetype="application/json",
    )


@app.route("/benchmark_story/start", methods=["POST"])
def benchmark_story_start() -> Response:
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
        started = story_benchmark_runner.start(
            app, target_uuids=target_uuids, bench_indices=bench_indices
        )
    except ValueError as e:
        abort(400, str(e))
    return app.response_class(
        json.dumps({"started": started}),
        mimetype="application/json",
    )


@app.route("/benchmark_story/stop", methods=["POST"])
def benchmark_story_stop() -> Response:
    story_benchmark_runner.stop()
    return app.response_class(
        json.dumps({"stopping": True}),
        mimetype="application/json",
    )
