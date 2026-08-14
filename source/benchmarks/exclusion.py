"""One benchmark at a time, across every runner.

Each runner (`BenchmarkRunner` for the general/kanban/story suites,
`BenchmarkEditDocumentRunner` for the edit-document one) guards its own
re-entry, but nothing used to stop /benchmark_story and /benchmark_kanban from
being started minutes apart and driving two models at once. The models run
locally on one consumer machine: two suites in flight means two models resident
and competing for the same GPU, which is slower than running them in turn and
makes every timing the benchmarks record meaningless.

So the runners share one slot. `start()` takes it or refuses; `_finish()`
releases it. An in-process lock is enough even though trials execute in child
processes, because the runner thread that spawned a child blocks until that
child is done — the slot is held for the whole row, not just the spawn.

The holder's label is kept so the page that lost the race can say which suite
is running rather than just greying out its button.
"""

from __future__ import annotations

import threading


class _BenchmarkSlot:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._holder: str | None = None

    def acquire(self, holder: str) -> bool:
        """Take the slot for `holder`. False if another runner already has it.

        Never blocks: a benchmark run is minutes long, so a caller that loses
        the race wants to be told, not parked."""
        with self._lock:
            if self._holder is not None:
                return False
            self._holder = holder
            return True

    def release(self, holder: str) -> None:
        """Give the slot back. A no-op unless `holder` is the current owner, so
        a late release from a finished run can't free someone else's slot."""
        with self._lock:
            if self._holder == holder:
                self._holder = None

    def holder(self) -> str | None:
        with self._lock:
            return self._holder


SLOT = _BenchmarkSlot()
