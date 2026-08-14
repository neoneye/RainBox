"""Only one benchmark suite may hold the machine at a time.

The models run locally on one consumer box: two suites in flight means two
models resident and competing for the same GPU, which makes every timing the
benchmarks record meaningless.
"""

import threading

import pytest
from flask import Flask

from benchmarks.exclusion import SLOT, _BenchmarkSlot


@pytest.fixture(autouse=True)
def clean_global_slot():
    """The runners share the module-level SLOT; leave it as we found it."""
    before = SLOT.holder()
    yield
    if before is None and SLOT.holder() is not None:
        SLOT.release(SLOT.holder() or "")


class TestSlot:
    def test_a_second_holder_is_refused(self):
        slot = _BenchmarkSlot()
        assert slot.acquire("Story benchmark") is True
        assert slot.acquire("Kanban benchmark") is False
        assert slot.holder() == "Story benchmark"

    def test_the_same_runner_cannot_take_it_twice(self):
        """Covers a page double-click as well as two pages racing."""
        slot = _BenchmarkSlot()
        assert slot.acquire("Story benchmark") is True
        assert slot.acquire("Story benchmark") is False

    def test_releasing_hands_it_to_the_next_caller(self):
        slot = _BenchmarkSlot()
        slot.acquire("Story benchmark")
        slot.release("Story benchmark")
        assert slot.holder() is None
        assert slot.acquire("Kanban benchmark") is True

    def test_a_stray_release_cannot_free_someone_elses_run(self):
        """A late release from a finished run must not open the door while
        another suite is mid-flight."""
        slot = _BenchmarkSlot()
        slot.acquire("Story benchmark")
        slot.release("Kanban benchmark")
        assert slot.holder() == "Story benchmark"

    def test_exactly_one_of_many_racing_threads_wins(self):
        slot = _BenchmarkSlot()
        won: list[str] = []
        start = threading.Barrier(8)

        def contend(i: int) -> None:
            start.wait()
            if slot.acquire(f"runner-{i}"):
                won.append(f"runner-{i}")

        threads = [threading.Thread(target=contend, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(won) == 1
        assert slot.holder() == won[0]


class TestRunnersShareIt:
    def test_every_runner_has_a_distinct_label(self):
        """The label is what a blocked page shows, so it must name the suite
        that is actually running."""
        from benchmarks.editdocument_runner import BenchmarkEditDocumentRunner
        from benchmarks.runner import SPEC_SET_LABELS

        labels = list(SPEC_SET_LABELS.values()) + [
            BenchmarkEditDocumentRunner.label
        ]
        assert len(labels) == len(set(labels))
        assert all(labels)

    def test_a_started_runner_blocks_the_other_suites(self, monkeypatch):
        """The whole point: /benchmark_story running must stop
        /benchmark_kanban from starting a second model."""
        from benchmarks.runner import BenchmarkRunner

        app = Flask(__name__)
        story = BenchmarkRunner(spec_set="story")
        kanban = BenchmarkRunner(spec_set="kanban")

        # Stand in for the worker thread: hold the slot without spawning one.
        monkeypatch.setattr(BenchmarkRunner, "_begin_run", lambda *a, **kw: None)

        assert story.start(app) is True
        try:
            assert kanban.start(app) is False
            assert kanban.get_state()["blocked_by"] == "Story benchmark"
            # The holder itself is never told it is blocked.
            assert story.get_state()["blocked_by"] is None
        finally:
            SLOT.release(story.label)
        assert kanban.get_state()["blocked_by"] is None
        assert kanban.start(app) is True
        SLOT.release(kanban.label)

    def test_a_failed_start_does_not_strand_the_slot(self, monkeypatch):
        """_collect_targets touches the DB and _validate_bench_indices raises
        on a bad index; either way no worker thread exists yet to release, so
        a leak here would lock every benchmark page out until restart."""
        from benchmarks.runner import BenchmarkRunner

        app = Flask(__name__)
        runner = BenchmarkRunner(spec_set="story")

        def boom(*a, **kw):
            raise ValueError("bad bench index")

        monkeypatch.setattr(BenchmarkRunner, "_begin_run", boom)
        with pytest.raises(ValueError):
            runner.start(app)
        assert SLOT.holder() is None
