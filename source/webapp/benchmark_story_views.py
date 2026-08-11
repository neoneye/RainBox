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
        "Plain text. The request hands the model a fresh random number and it "
        "must appear in that section, as digits. Correct iff every section is "
        "within the word band, contains its number, and is not a copy of an "
        "earlier one."
    ),
    "story_struct": (
        "Structured output: `section_text` (the piece) plus `section_reviewer`, "
        "a brutally harsh critique of that same section. The request hands over "
        "the number and it belongs in `section_text`, not in the critique. "
        "Correct iff both fields are within their own word bands and every "
        "section contains its number."
    ),
    "story_text_tool": (
        "Plain text, but the model has to fetch the number rather than being "
        "given it: before each section it must call `random_number` exactly "
        "once and work the returned integer into the prose. Correct iff every "
        "section made exactly one call and contains its number. Identical to "
        "story_text apart from where the number comes from, so the gap between "
        "their scores isolates tool calling. Requires a function-calling "
        "target."
    ),
    "story_struct_tool": (
        "The crossover: fetch the number by tool call AND return the two-field "
        "object, on every one of the five turns. Usually the hardest of the "
        "four for a local model, and the one where a model that manages either "
        "task alone can still come apart doing both."
    ),
}

STORY_INTRO = (
    "A workload built to exercise the prompt cache. Every turn resends the "
    "system prompt and the whole conversation so far and appends one new "
    "message, so each prompt is a strict prefix extension of the last — the "
    "shape a KV cache is built for, and the shape rainbox itself produces in "
    "the assistant loop, chat and the kanban workers. One-shot benchmarks give "
    "a cache nothing to hold; five-turn conversations give it everything. "
    "Run a target here, then read the reusable-prefix rate on /activity: that "
    "number is only meaningful against a workload that should reuse almost "
    "everything, and this is that workload. "
    "\n\n"
    "Each section also has to carry a fresh random integer, which is what "
    "makes the run checkable at all. \"Did it write a good section\" is a "
    "matter of taste; \"do the digits the model was handed appear in the "
    "section it then wrote\" is not. The number is generated per turn, never "
    "appears in the system prompt, and differs every section — so it cannot be "
    "satisfied from memory, by copying an example, or by reusing an earlier "
    "one. Two of the benchmarks hand the number over in the request; the other "
    "two make the model call a tool to get it. Everything else about them is "
    "the same, so the difference between their scores is the cost of the tool "
    "call and nothing else. "
    "\n\n"
    "Three trials each, each on a different brief. A section is also failed "
    "for running far outside the word band or for reproducing an earlier "
    "section verbatim — a model that stops advancing and replays itself is a "
    "thing worth catching, not a clean run. "
    "Hover a Copy button to see which brief it holds; click to put the piece "
    "on the clipboard, or take the json for the full exchange: system prompt, "
    "every request and response, and what the number did on each turn. Every "
    "section heading carries its own verdict, so a failed trial can be read "
    "rather than re-run."
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
