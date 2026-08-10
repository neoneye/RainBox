"""The /benchmark_story page — ten-turn conversation benchmarks.

Reuses the shared benchmark-suite page + BenchmarkRunner machinery from
benchmark_views/benchmarks.runner; only the spec set, runner instance, and
endpoints differ. The one addition is `show_artifacts`, which turns on the
per-trial Copy buttons — these trials produce a story, and a story is worth
reading.
"""

import json

from flask import Response, request

from benchmarks.runner import STORY_BENCHMARK_SPECS

from .benchmark_views import render_benchmark_page
from .core import app, story_benchmark_runner

STORY_BENCHMARK_DESCRIPTIONS: dict[str, str] = {
    "story_text": (
        "Ten turns of plain text. Each turn asks for the next ~200-word "
        "section of a horror story, resending the whole conversation. "
        "Correct iff all ten sections arrive within 100–350 words."
    ),
    "story_struct": (
        "Same ten turns, structured output: `section_text` (the prose) plus "
        "`section_reviewer`, a brutally harsh critique of that same section. "
        "Correct iff every section is in range and every critique is present."
    ),
    "story_text_tool": (
        "Same ten turns, plus one tool. Before each section the model must "
        "call `omen_number`, which returns a random integer that must then "
        "appear literally in the prose. Correct iff every section called the "
        "tool and contains its number — which a model that calls the tool and "
        "ignores the answer cannot fake. Requires a function-calling target."
    ),
    "story_struct_tool": (
        "The crossover: structured output AND function calling on every one "
        "of the ten turns. The hardest of the four for a local model."
    ),
}

STORY_INTRO = (
    "Ten-turn conversations that write a horror story a section at a time, "
    "across the text/structured × no-tools/tools matrix. Every turn resends "
    "the system prompt and the whole history and appends one message, so each "
    "prompt is a strict prefix extension of the last — the shape a KV cache "
    "is built for. That makes this suite both a capability test and the "
    "workload that puts a real number on the /activity dashboard: check the "
    "reusable-prefix rate there after a run. "
    "Three trials rather than five, because each trial is ten LLM calls — "
    "budget roughly 20–40 minutes per target for the full set. Use the Copy "
    "buttons on a finished cell to read what the model actually wrote."
)


@app.route("/benchmark_story")
def benchmark_story_page() -> str:
    return render_benchmark_page(
        "Benchmark story", STORY_INTRO,
        STORY_BENCHMARK_SPECS, STORY_BENCHMARK_DESCRIPTIONS,
        "benchmark_story_state", "benchmark_story_start", "benchmark_story_stop",
        show_artifacts=True,
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
    started = story_benchmark_runner.start(app, target_uuids=target_uuids)
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
