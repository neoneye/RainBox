"""Running a subset: one benchmark for one target, without disturbing the rest.

The story suite runs four benchmarks in order, and the interesting one is
often last — waiting through story_text and story_struct to reach
story_text_tool is minutes of nothing. A cell has to be runnable on its own.
"""

import pytest

from benchmarks.runner import BenchmarkRunner, STORY_BENCHMARK_SPECS


@pytest.fixture
def runner():
    r = BenchmarkRunner(spec_set="story")
    r._state["targets"] = [
        {
            "index": 0,
            "uuid": "aaaa",
            "model_name": "m",
            "display_name": "d",
            "status": "done",
            "benchmarks": [
                {
                    "name": name,
                    "status": "done",
                    "trials_done": 3,
                    "trials_total": 3,
                    "correct": 2,
                    "mistakes": 1,
                    "failures": 0,
                    "total_elapsed": 9.0,
                    "error": None,
                    "reasoning_chars": None,
                    "content_chars": None,
                    "stories": [{"trial": 0, "correct": True, "topic": "t"}],
                }
                for name, _cls, _kw in STORY_BENCHMARK_SPECS
            ],
        }
    ]
    return r


class TestBenchIndices:
    def test_the_worker_request_names_the_chosen_benchmarks(self, runner):
        assert runner._worker_request("aaaa", False, [2]) == {
            "target_uuid": "aaaa",
            "skip_warmup": False,
            "spec_set": "story",
            "bench_indices": [2],
        }

    def test_no_selection_means_every_benchmark(self, runner):
        """The whole-row and whole-sweep buttons must keep working."""
        request = runner._worker_request("aaaa", False, None)
        assert request["bench_indices"] is None

    def test_resetting_one_cell_leaves_the_others_alone(self, runner):
        """Re-running story_text_tool must not wipe the story_text results
        sitting next to it — the operator ran those and wants to keep them."""
        runner._reset_for_run(0, [2])
        entries = runner._state["targets"][0]["benchmarks"]
        assert entries[2]["status"] == "pending"
        assert entries[2]["correct"] == 0
        assert entries[2]["stories"] == []
        for keep in (0, 1, 3):
            assert entries[keep]["status"] == "done"
            assert entries[keep]["correct"] == 2
            assert entries[keep]["stories"]

    def test_resetting_the_whole_row_clears_every_cell(self, runner):
        runner._reset_for_run(0, None)
        for entry in runner._state["targets"][0]["benchmarks"]:
            assert entry["status"] == "pending"
            assert entry["correct"] == 0

    def test_an_out_of_range_index_is_refused(self, runner):
        """The index comes from a query string and picks a spec by position."""
        with pytest.raises(ValueError):
            runner._reset_for_run(0, [99])

    def test_a_negative_index_is_refused(self, runner):
        with pytest.raises(ValueError):
            runner._reset_for_run(0, [-1])
