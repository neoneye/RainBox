"""pytest plugin for the rainbox suite: a progress bar with an ETA, a guard
that keeps tests off live models, and LLM KPIs for the calls that do happen.

Registered from the root conftest (`pytest_plugins`), so it is on for every
run without flags.

Progress
    One line, redrawn in place on the terminal:

        [██████████░░░░░░░░░░] 1234/3923 31% · 01:12 · ETA 02:40 · agents/test_x.py::test_y 2.4s

    Tests that finish fast only move the bar; a test that takes longer than
    `SLOW_SECONDS` is named on the line while it runs, with its running time,
    so a stalled run and a slow test look different from a run that has
    stopped. The line is drawn on `/dev/tty` directly — pytest captures file
    descriptors 1 and 2 during a test, and the redraw has to reach the
    terminal while a test is still running. Without a terminal (a log file,
    CI) the plugin prints one plain line every 5 % and one for each slow
    test instead.

Live-model guard
    Tests stub the model seams; a test that reaches a real model is a
    mistake, and an expensive one — a local model at a 131 072-token context
    turns a millisecond test into a minute and pins 25 GB. The guard blocks
    every socket connection to the local model ports (Ollama, LM Studio, Unsloth Studio) and
    every connection that leaves the loopback interface, raising a
    `ConnectionRefusedError` that names the test so the offender is one
    line of output away. Postgres on loopback stays reachable. Set
    `RAINBOX_TEST_LIVE_MODELS=1` to lift the guard for a run that means to
    use a model.

KPIs
    Every model call is counted through the same recorder production uses
    (`llm.activity`), with an in-memory sink: calls, prompt and completion
    tokens, prefill and decode throughput. The totals print in the terminal
    summary, with the tests that made calls listed by name — under the
    guard that list must be empty.
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
import threading
import time
from typing import Any

import pytest

LIVE_ENV = "RAINBOX_TEST_LIVE_MODELS"
MODEL_PORTS = frozenset({11434, 1234, 8888})    # Ollama, LM Studio, Unsloth Studio
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", ""})
SLOW_SECONDS = 2.0
REDRAW_SECONDS = 0.5
BAR_WIDTH = 20


# ---- helpers -------------------------------------------------------------

def _fmt_clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _fmt_tokens(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 10_000 else str(n)


def _address_parts(address: Any) -> tuple[str, int | None]:
    """(host, port) of a socket address; unix sockets read as ("", None)."""
    if isinstance(address, tuple) and len(address) >= 2:
        return str(address[0]).lower(), int(address[1])
    return "", None


def _blocked(address: Any) -> str | None:
    """Why a connection to `address` is refused under the guard, or None."""
    host, port = _address_parts(address)
    if port is None:
        return None
    if port in MODEL_PORTS:
        return f"{host}:{port} is a model port"
    if host not in LOOPBACK_HOSTS:
        return f"{host}:{port} is not loopback"
    return None


class _NoHistory:
    """The recorder's per-model lookups, answered with nothing: the KPI
    recorder has no baseline to compare against and needs none."""

    def recent_throughputs(self, model: str | None) -> list[float]:
        return []

    def recent_prefix_chains(self, model: str | None) -> list[list[str]]:
        return []


# ---- the plugin ----------------------------------------------------------

class RainboxSuite:
    def __init__(self, config: pytest.Config) -> None:
        self.config = config
        self.verbose = config.option.verbose > 0
        self.tty = self._open_tty()
        self.total = 0
        self.done = 0
        self.failed = 0
        self.started = 0.0
        self.current: str | None = None
        self.current_started = 0.0
        self.durations: list[tuple[float, str]] = []
        self.last_draw = 0.0
        self.last_pct_printed = -1
        self.lock = threading.Lock()
        self.stop = threading.Event()
        # LLM KPIs: totals and the per-test attribution.
        self.calls = 0
        self.calls_with_usage = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.prefill_ms = 0
        self.decode_ms = 0
        self.calls_at_test_start = 0
        self.tests_with_calls: list[tuple[str, int]] = []
        self.guarded = not os.environ.get(LIVE_ENV)
        self.guard_hits: list[str] = []
        self._real_connect = socket.socket.connect
        self._real_connect_ex = socket.socket.connect_ex
        self._guard_installed = False

    # -- terminal ---------------------------------------------------------

    @staticmethod
    def _open_tty():
        if not sys.stdout.isatty():
            return None
        try:
            return open("/dev/tty", "w", buffering=1)
        except OSError:
            return None

    def _bar(self) -> str:
        frac = self.done / self.total if self.total else 0.0
        filled = int(BAR_WIDTH * frac)
        return "█" * filled + "░" * (BAR_WIDTH - filled)

    def _status(self, now: float) -> str:
        elapsed = now - self.started
        if self.done:
            eta = elapsed / self.done * (self.total - self.done)
            eta_text = f"ETA {_fmt_clock(eta)}"
        else:
            eta_text = "ETA --:--"
        pct = int(100 * self.done / self.total) if self.total else 0
        parts = [f"[{self._bar()}] {self.done}/{self.total} {pct}%",
                 _fmt_clock(elapsed), eta_text]
        if self.failed:
            parts.append(f"{self.failed} failed")
        if self.calls_with_usage:
            parts.append(f"llm {self.calls_with_usage} calls "
                         f"{_fmt_tokens(self.prompt_tokens)} in "
                         f"{_fmt_tokens(self.completion_tokens)} out")
        if self.current and now - self.current_started >= SLOW_SECONDS:
            parts.append(f"{self.current} {now - self.current_started:.0f}s")
        return " · ".join(parts)

    def _draw(self, now: float | None = None, force: bool = False) -> None:
        now = time.monotonic() if now is None else now
        if self.tty is None:
            self._draw_plain(now, force)
            return
        if not force and now - self.last_draw < REDRAW_SECONDS:
            return
        self.last_draw = now
        width = shutil.get_terminal_size((120, 24)).columns
        text = self._status(now)[:max(20, width - 1)]
        self.tty.write("\r\x1b[2K" + text)
        self.tty.flush()

    def _draw_plain(self, now: float, force: bool) -> None:
        """No terminal: a line at every 5 % and for each slow test."""
        pct = int(100 * self.done / self.total) if self.total else 0
        step = pct - pct % 5
        if force or step > self.last_pct_printed:
            self.last_pct_printed = step
            sys.stderr.write(self._status(now) + "\n")
            sys.stderr.flush()

    def _clear(self) -> None:
        if self.tty is not None:
            self.tty.write("\r\x1b[2K")
            self.tty.flush()

    def _heartbeat(self) -> None:
        """Redraw while a test runs so a slow test is visibly still going."""
        while not self.stop.wait(REDRAW_SECONDS):
            with self.lock:
                now = time.monotonic()
                if self.current and now - self.current_started >= SLOW_SECONDS:
                    self._draw(now, force=True)

    # -- pytest hooks -------------------------------------------------------

    def pytest_sessionstart(self, session: pytest.Session) -> None:
        self._install_guard()
        self._install_kpi_recorder()

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        self.total = len(session.items)
        self.started = time.monotonic()
        threading.Thread(target=self._heartbeat, name="rainbox-progress",
                         daemon=True).start()
        self._draw(force=True)

    def pytest_runtest_logstart(self, nodeid: str, location: Any) -> None:
        with self.lock:
            self.current = nodeid
            self.current_started = time.monotonic()
            self.calls_at_test_start = self.calls_with_usage

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        finished = report.when == "call" or (
            report.when == "setup" and report.outcome != "passed")
        if not finished:
            return
        with self.lock:
            now = time.monotonic()
            self.done += 1
            if report.failed:
                self.failed += 1
            took = now - self.current_started
            self.durations.append((took, report.nodeid))
            made = self.calls_with_usage - self.calls_at_test_start
            if made:
                self.tests_with_calls.append((report.nodeid, made))
            slow = took >= SLOW_SECONDS
            self.current = None
            if self.tty is None and slow:
                sys.stderr.write(f"slow: {report.nodeid} {took:.1f}s\n")
            self._draw(now, force=slow)

    @pytest.hookimpl(tryfirst=True)
    def pytest_report_teststatus(self, report: pytest.TestReport, config):
        # The bar replaces the dots; failures and errors keep their letter
        # and the verbose word column stays intact.
        if (self.tty is not None and not self.verbose and report.passed
                and report.when == "call"):
            return "passed", "", "PASSED"
        return None

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        self.stop.set()
        self._clear()
        self._remove_guard()

    def pytest_terminal_summary(self, terminalreporter: Any) -> None:
        tr = terminalreporter
        elapsed = time.monotonic() - self.started if self.started else 0.0
        tr.ensure_newline()
        tr.section("rainbox suite", sep="-")
        tr.write_line(f"{self.done} tests in {_fmt_clock(elapsed)}"
                      + (f" · {self.failed} failed" if self.failed else ""))
        slowest = sorted(self.durations, reverse=True)[:10]
        if slowest and slowest[0][0] >= 1.0:
            tr.write_line("slowest:")
            for took, nodeid in slowest:
                if took < 1.0:
                    break
                tr.write_line(f"  {took:6.1f}s  {nodeid}")
        fakes = self.calls - self.calls_with_usage
        if self.calls_with_usage:
            decode_tps = (self.completion_tokens * 1000 / self.decode_ms
                          if self.decode_ms else None)
            prefill_tps = (self.prompt_tokens * 1000 / self.prefill_ms
                           if self.prefill_ms else None)
            tr.write_line(
                f"llm: {self.calls_with_usage} model calls · "
                f"{self.prompt_tokens} tokens in · "
                f"{self.completion_tokens} tokens out"
                + (f" · prefill {prefill_tps:.0f} tok/s" if prefill_tps else "")
                + (f" · decode {decode_tps:.0f} tok/s" if decode_tps else "")
                + (f" · {fakes} fake-model calls" if fakes else ""))
            for nodeid, made in self.tests_with_calls:
                tr.write_line(f"  {made:3d} calls  {nodeid}")
        else:
            tr.write_line("llm: no model calls"
                          + (f" ({fakes} fake-model calls)" if fakes else "")
                          + ("" if self.guarded else f" · {LIVE_ENV} set"))
        if self.guard_hits:
            tr.write_line(f"live-model guard refused {len(self.guard_hits)} "
                          f"connection(s):")
            for hit in self.guard_hits[:20]:
                tr.write_line(f"  {hit}")

    # -- live-model guard -----------------------------------------------------

    def _install_guard(self) -> None:
        if not self.guarded:
            return
        plugin = self
        self._real_connect = socket.socket.connect
        self._real_connect_ex = socket.socket.connect_ex
        self._guard_installed = True

        def connect(sock, address):
            plugin._check(address)
            return plugin._real_connect(sock, address)

        def connect_ex(sock, address):
            plugin._check(address)
            return plugin._real_connect_ex(sock, address)

        socket.socket.connect = connect        # type: ignore[method-assign]
        socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]

    def _remove_guard(self) -> None:
        if self._guard_installed:
            socket.socket.connect = self._real_connect        # type: ignore[method-assign]
            socket.socket.connect_ex = self._real_connect_ex  # type: ignore[method-assign]
            self._guard_installed = False

    def _check(self, address: Any) -> None:
        why = _blocked(address)
        if why is None:
            return
        where = self.current or "(outside a test)"
        with self.lock:
            self.guard_hits.append(f"{why} — {where}")
        raise ConnectionRefusedError(
            f"rainbox tests must not reach a live model: {why} (in {where}). "
            f"Stub the model seam, or set {LIVE_ENV}=1 for a run that means "
            f"to use one.")

    # -- KPIs ---------------------------------------------------------------------

    def _install_kpi_recorder(self) -> None:
        try:
            from llama_index.core.instrumentation import get_dispatcher
            from llm.activity import ActivityRecorder
        except Exception:
            return
        recorder = ActivityRecorder(sink=self._kpi_sink, history=_NoHistory())
        get_dispatcher().add_event_handler(recorder)

    def _kpi_sink(self, row: dict[str, Any]) -> None:
        """Count a finished call. A test's fake model answers through the
        same dispatcher with no usage data; only calls that report tokens
        are model calls in the KPI sense — the per-test list and the
        progress line count those."""
        with self.lock:
            self.calls += 1
            usage = (row.get("prompt_tokens") or 0) + (row.get("completion_tokens") or 0)
            if not usage:
                return
            self.calls_with_usage += 1
            self.prompt_tokens += int(row.get("prompt_tokens") or 0)
            self.completion_tokens += int(row.get("completion_tokens") or 0)
            self.prefill_ms += int(row.get("prefill_ms") or 0)
            self.decode_ms += int(row.get("decode_ms") or 0)


def pytest_configure(config: pytest.Config) -> None:
    if config.pluginmanager.has_plugin("rainbox-suite"):
        return
    config.pluginmanager.register(RainboxSuite(config), "rainbox-suite")
