"""The /benchmark_story page, and the copy-button affordance it adds to the
shared benchmark template without disturbing the two pages that don't want it.
"""

from unittest.mock import patch

import pytest

import webapp
from benchmarks.runner import STORY_BENCHMARK_SPECS
from webapp.core import story_benchmark_runner


@pytest.fixture
def client():
    webapp.app.config["TESTING"] = True
    with webapp.app.test_client() as c:
        with webapp.app.app_context():
            yield c


@pytest.fixture
def state_with_a_story():
    """Plant a finished run in the runner's state, as the worker would."""
    runner = story_benchmark_runner
    original = runner.get_state()
    runner._state = {
        "running": False,
        "started_at": None,
        "ended_at": None,
        "aborted": False,
        "current_target_index": -1,
        "total_targets": 1,
        "targets": [
            {
                "index": 0,
                "kind": "override",
                "uuid": "11111111-1111-1111-1111-111111111111",
                "provider": "ollama",
                "model_name": "fake-model",
                "model_display_name": "fake-model",
                "display_name": "test target",
                "status": "done",
                "warmup_elapsed": 1.0,
                "warmup_started_at": None,
                "benchmarks": [
                    {
                        "name": name,
                        "status": "done",
                        "trials_done": 1,
                        "trials_total": 3,
                        "correct": 1,
                        "mistakes": 0,
                        "failures": 0,
                        "total_elapsed": 12.0,
                        "error": None,
                        "reasoning_chars": None,
                        "content_chars": None,
                        "stories": (
                            [{"trial": 0, "correct": True, "topic": "A brief"}]
                            if name == "story_text"
                            else []
                        ),
                    }
                    for name, _cls, _kw in STORY_BENCHMARK_SPECS
                ],
            }
        ],
    }
    yield runner
    runner._state = original


class TestPage:
    def test_the_page_loads(self, client):
        assert client.get("/benchmark_story").status_code == 200

    def test_all_four_benchmarks_are_listed(self, client):
        body = client.get("/benchmark_story").get_data(as_text=True)
        for name, _cls, _kw in STORY_BENCHMARK_SPECS:
            assert name in body

    def test_it_appears_in_the_benchmark_nav(self, client):
        assert ">Story<" in client.get("/benchmark_story").get_data(as_text=True)

    def test_the_intro_leads_with_the_cache(self, client):
        """That is what the suite is for; the story is the vehicle."""
        body = client.get("/benchmark_story").get_data(as_text=True)
        assert "prompt cache" in body
        assert "prefix extension" in body

    def test_the_intro_explains_the_number_mechanism(self, client):
        """Why the run is checkable at all, rather than a matter of taste."""
        body = client.get("/benchmark_story").get_data(as_text=True)
        assert "fresh random integer" in body
        assert "fetched by tool call" in body

    def test_the_intro_quotes_no_timing(self, client):
        """It varies far too much by model to be worth stating."""
        body = client.get("/benchmark_story").get_data(as_text=True)
        assert "minutes per target" not in body

    def test_the_intro_points_at_the_activity_dashboard(self, client):
        body = client.get("/benchmark_story").get_data(as_text=True)
        assert "/activity" in body


class TestArtifacts:
    def test_copy_buttons_are_enabled_on_this_page(self, client):
        body = client.get("/benchmark_story").get_data(as_text=True)
        assert "const SHOW_ARTIFACTS = true" in body

    def test_they_stay_off_on_the_other_benchmark_pages(self, client):
        """The general and kanban suites produce nothing to copy; turning the
        affordance on there would render an empty control."""
        for path in ("/benchmark_basic", "/benchmark_kanban"):
            body = client.get(path).get_data(as_text=True)
            assert "const SHOW_ARTIFACTS = false" in body

    def test_a_refused_clipboard_tells_the_operator(self, client):
        """The Clipboard API rejects on an unfocused document and is absent
        over plain http to a remote host. Without a rejection path the button
        does nothing at all, and the operator pastes whatever was on the
        clipboard before — which reads as the wrong story, not as a failure."""
        body = client.get("/benchmark_story").get_data(as_text=True)
        assert "copy failed" in body
        assert "legacyCopy" in body

    def test_the_inline_script_survived_python_string_escaping(self, client):
        """BENCHMARK_TEMPLATE is a plain, non-raw Python string, so a
        backslash escape in the JS would be eaten before the browser saw it."""
        from webapp.benchmark_views import BENCHMARK_TEMPLATE

        assert "\\" not in BENCHMARK_TEMPLATE

    def test_the_other_pages_still_render(self, client):
        assert client.get("/benchmark_basic").status_code == 200
        assert client.get("/benchmark_kanban").status_code == 200


class TestStateEndpoint:
    def test_state_is_json(self, client):
        r = client.get("/benchmark_story/state")
        assert r.status_code == 200
        assert r.get_json()["targets"] is not None

    def test_the_state_the_page_polls_is_json_serialisable(self, client):
        """Stories are plain strings on the entry, so the whole state must
        survive json.dumps — a dataclass slipping in would 500 the poll."""
        import json

        json.dumps(story_benchmark_runner.get_state())

    def test_the_state_carries_a_descriptor_not_the_text(self, state_with_a_story):
        """The page polls this about once a second; the artifacts do not ride
        along."""
        state = state_with_a_story.get_state()
        bench = state["targets"][0]["benchmarks"][0]
        assert bench["stories"][0]["topic"] == "A brief"
        assert "text" not in bench["stories"][0]
        assert "transcript" not in bench["stories"][0]

    def test_benchmarks_without_a_story_carry_an_empty_list(self, state_with_a_story):
        state = state_with_a_story.get_state()
        assert state["targets"][0]["benchmarks"][1]["stories"] == []


class TestArtifactEndpoint:
    """Artifacts are fetched, not polled — so the endpoint has to behave."""

    @pytest.fixture
    def recorded(self):
        runner = story_benchmark_runner
        runner._record_story(
            0, 0, 0, "## Section 1 - Correct\n\nthe door", True,
            topic="A brief", transcript={"benchmark": "story_text", "turns": []},
        )
        yield
        runner._artifacts.clear()

    def test_markdown_is_served_as_text(self, client, recorded):
        r = client.get("/benchmark_story/artifact?target=0&bench=0&trial=0")
        assert r.status_code == 200
        assert "the door" in r.get_data(as_text=True)

    def test_json_is_served_as_a_download(self, client, recorded):
        """A file to open in an editor next to the code, not a tab to squint
        at."""
        r = client.get(
            "/benchmark_story/artifact?target=0&bench=0&trial=0&format=json"
        )
        assert r.status_code == 200
        assert r.mimetype == "application/json"
        assert "attachment" in r.headers["Content-Disposition"]
        assert r.get_json()["benchmark"] == "story_text"

    def test_a_missing_trial_is_a_404_not_a_500(self, client):
        r = client.get("/benchmark_story/artifact?target=9&bench=9&trial=9")
        assert r.status_code == 404

    def test_junk_indices_are_refused(self, client):
        r = client.get("/benchmark_story/artifact?target=x&bench=0&trial=0")
        assert r.status_code == 400

    def test_missing_indices_are_refused(self, client):
        assert client.get("/benchmark_story/artifact").status_code == 400


class TestRunnerWiring:
    def test_the_page_has_its_own_runner_instance(self):
        """Sharing one with /benchmark_basic would let a story sweep and a
        general sweep clobber each other's state."""
        from webapp.core import benchmark_runner, kanban_benchmark_runner

        assert story_benchmark_runner is not benchmark_runner
        assert story_benchmark_runner is not kanban_benchmark_runner
        assert story_benchmark_runner.spec_set == "story"

    def test_a_fresh_benchmark_entry_has_a_stories_list(self):
        from benchmarks.runner import _empty_benchmark_entry

        assert _empty_benchmark_entry("story_text", 3)["stories"] == []

    def test_the_runner_records_a_story_event(self):
        from benchmarks.runner import BenchmarkRunner

        runner = BenchmarkRunner(spec_set="story")
        runner._state["targets"] = [
            {"benchmarks": [_bench_entry(), _bench_entry()]}
        ]
        runner._apply_event(
            0,
            {"t": "story", "bi": 1, "trial": 2, "text": "boo", "correct": False,
             "topic": "A user manual for grief",
             "transcript": {"benchmark": "story_text"}},
        )
        # The state carries only a descriptor — the page polls it every second.
        stored = runner._state["targets"][0]["benchmarks"][1]["stories"]
        assert stored == [
            {"trial": 2, "correct": False, "topic": "A user manual for grief"}
        ]
        # The bulky artifacts live beside it, fetched on request.
        artifact = runner.get_artifact(0, 1, 2)
        assert artifact["markdown"] == "boo"
        assert artifact["transcript"] == {"benchmark": "story_text"}

    def test_an_unrecorded_trial_has_no_artifact(self):
        from benchmarks.runner import BenchmarkRunner

        assert BenchmarkRunner(spec_set="story").get_artifact(0, 0, 0) is None

    def test_a_story_event_without_a_topic_still_records(self):
        """Older workers, and any suite that grows an artifact later, send no
        topic — the runner must not require one."""
        from benchmarks.runner import BenchmarkRunner

        runner = BenchmarkRunner(spec_set="story")
        runner._state["targets"] = [{"benchmarks": [_bench_entry()]}]
        runner._apply_event(
            0, {"t": "story", "bi": 0, "trial": 0, "text": "boo", "correct": True}
        )
        assert runner._state["targets"][0]["benchmarks"][0]["stories"][0]["topic"] == ""


def _bench_entry():
    from benchmarks.runner import _empty_benchmark_entry

    return _empty_benchmark_entry("story_text", 3)


class TestPerCellStart:
    """A cell has to be runnable on its own: the story suite runs four
    benchmarks in order and the one worth watching is often last."""

    def test_every_cell_offers_a_start(self, client):
        body = client.get("/benchmark_story").get_data(as_text=True)
        assert "cell-start" in body
        assert 'data-bench="${bi}"' in body

    def test_the_row_and_sweep_buttons_survive(self, client):
        body = client.get("/benchmark_story").get_data(as_text=True)
        assert "row-start" in body
        assert 'id="start-btn"' in body

    def test_one_click_handler_serves_both(self, client):
        """A second listener would double-fire when the markup is rebuilt by
        polling."""
        body = client.get("/benchmark_story").get_data(as_text=True)
        assert "button.row-start, button.cell-start" in body

    def test_the_endpoint_accepts_a_bench_index(self, client, monkeypatch):
        seen = {}

        def fake_start(app, target_uuids=None, bench_indices=None, warmup=True):
            seen["targets"] = target_uuids
            seen["benches"] = bench_indices
            return True

        monkeypatch.setattr(story_benchmark_runner, "start", fake_start)
        r = client.post("/benchmark_story/start?target_uuid=abc&bench=2")
        assert r.status_code == 200
        assert seen == {"targets": ["abc"], "benches": [2]}

    def test_no_bench_index_still_runs_the_whole_row(self, client, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            story_benchmark_runner,
            "start",
            lambda app, target_uuids=None, bench_indices=None, warmup=True: seen.update(
                benches=bench_indices
            )
            or True,
        )
        client.post("/benchmark_story/start?target_uuid=abc")
        assert seen["benches"] is None

    def test_a_junk_bench_index_is_refused(self, client):
        r = client.post("/benchmark_story/start?target_uuid=abc&bench=nope")
        assert r.status_code == 400

    def test_an_out_of_range_bench_index_is_refused(self, client):
        """It selects a spec by position, so it cannot be trusted raw."""
        r = client.post("/benchmark_story/start?target_uuid=abc&bench=99")
        assert r.status_code == 400


class TestWarmupToggle:
    """The pre-trial "hi" call is what makes a model warm before the first
    trial — exactly the thing being observed when the run is read for cache
    behaviour, so the story page can turn it off."""

    def test_the_story_page_offers_the_toggle(self):
        body = webapp.app.test_client().get("/benchmark_story").get_data(as_text=True)
        assert 'id="warmup-toggle"' in body
        assert "Warm up LLM" in body

    def test_it_is_unchecked_in_the_markup(self):
        """Default off; localStorage is what turns it back on for a session
        that wants it."""
        body = webapp.app.test_client().get("/benchmark_story").get_data(as_text=True)
        assert '<input type="checkbox" id="warmup-toggle">' in body

    def test_the_other_suites_do_not_show_it(self):
        """They are read for timings, where a cold model's load time landing
        inside the first average is the problem the warmup solves."""
        for path in ("/benchmark_basic", "/benchmark_kanban"):
            body = webapp.app.test_client().get(path).get_data(as_text=True)
            assert 'id="warmup-toggle"' not in body, path

    def test_warmup_0_reaches_the_runner_as_false(self):
        seen = {}

        def fake_start(_app, target_uuids=None, bench_indices=None, warmup=True):
            seen["warmup"] = warmup
            return True

        with patch.object(story_benchmark_runner, "start", fake_start):
            webapp.app.test_client().post("/benchmark_story/start?warmup=0")
        assert seen["warmup"] is False

    def test_warmup_1_reaches_the_runner_as_true(self):
        seen = {}

        def fake_start(_app, target_uuids=None, bench_indices=None, warmup=True):
            seen["warmup"] = warmup
            return True

        with patch.object(story_benchmark_runner, "start", fake_start):
            webapp.app.test_client().post("/benchmark_story/start?warmup=1")
        assert seen["warmup"] is True

    def test_an_absent_param_keeps_warming_up(self):
        """A hand-made request, or any caller that predates the toggle."""
        seen = {}

        def fake_start(_app, target_uuids=None, bench_indices=None, warmup=True):
            seen["warmup"] = warmup
            return True

        with patch.object(story_benchmark_runner, "start", fake_start):
            webapp.app.test_client().post("/benchmark_story/start")
        assert seen["warmup"] is True
