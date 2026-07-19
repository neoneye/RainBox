"""Tests for the Pyodide sandbox: computation works, escapes are blocked, and
the CPU/memory/wall-clock kill limits actually kill.

Each run_python call spawns a fresh node+pyodide process (~1.5s), so the escape
checks are batched into one job. The whole module skips when node or the npm
install is missing.
"""

import json
import shutil
from uuid import uuid4

import pytest

from agents.assistant import AssistantActionContext, _action_python_run
from tools.python_sandbox.sandbox import PYODIDE_DIR, run_python

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not PYODIDE_DIR.is_dir(),
    reason="python sandbox unavailable: needs node + `npm install` in tools/python_sandbox",
)


# --- computation --------------------------------------------------------------


def test_math_and_string_manipulation():
    result = run_python(
        "import re\n"
        "print('stress'[::-1])\n"
        "print(re.findall(r'\\d+', 'a1b22c333'))\n"
        "123456789 * 987654321"
    )
    assert result.ok
    assert result.stdout == "sserts\n['1', '22', '333']\n"
    assert result.result_repr == "121932631112635269"
    assert result.error is None


def test_exception_returns_user_traceback():
    result = run_python("def boom():\n    return 1 / 0\nboom()")
    assert not result.ok
    assert result.error is not None
    assert "ZeroDivisionError" in result.error
    assert '"<job>", line 2' in result.error


def test_fresh_state_between_jobs():
    assert run_python("leak = 42").ok
    result = run_python("leak")
    assert not result.ok
    assert "NameError" in (result.error or "")


# --- sandbox escapes ----------------------------------------------------------


def test_escape_vectors_all_blocked():
    """One batched job probing every known escape hatch; each entry must come
    back blocked. Values are exception type names (or NONE for absent attrs) —
    anything else means the vector is open."""
    result = run_python(
        """
import json, os
checks = {}

def attempt(name, fn):
    try:
        checks[name] = fn()
    except Exception as exc:
        checks[name] = type(exc).__name__

attempt("etc_passwd", lambda: open("/etc/passwd").read() and "OPEN")
attempt("host_fs", lambda: os.listdir("/Users") and "LISTED")

import js
attempt("js_process", lambda: "HAS" if hasattr(js, "process") else "NONE")
attempt("js_fetch", lambda: "HAS" if hasattr(js, "fetch") else "NONE")
attempt("js_eval", lambda: "HAS" if hasattr(js, "eval") else "NONE")

import pyodide_js
for prop in ("loadPackage", "mountNodeFS", "FS", "_module", "_api"):
    attempt("pyodide_js." + prop,
            lambda p=prop: "HAS" if getattr(pyodide_js, p, None) else "NONE")

attempt("micropip", lambda: __import__("micropip") and "IMPORTED")

def try_run_js():
    from pyodide.code import run_js
    return run_js("typeof process") and "RAN"
attempt("run_js", try_run_js)

# Emscripten fakes socket.connect() success, so probe actual data flow:
# WASM sockets would need a WebSocket transport, which jsglobals omits.
attempt("js_websocket", lambda: "HAS" if hasattr(js, "WebSocket") else "NONE")

def try_socket_data():
    import socket
    s = socket.socket()
    s.settimeout(3)
    s.connect(("example.com", 80))
    s.send(b"GET / HTTP/1.0\\r\\nHost: example.com\\r\\n\\r\\n")
    return "GOT " + repr(s.recv(200))
attempt("socket_data", try_socket_data)

print(json.dumps(checks))
"""
    )
    assert result.ok, result.error
    checks = json.loads(result.stdout)
    expected = {
        "etc_passwd": "FileNotFoundError",
        "host_fs": "FileNotFoundError",
        "js_process": "NONE",
        "js_fetch": "NONE",
        "js_eval": "NONE",
        "pyodide_js.loadPackage": "NONE",
        "pyodide_js.mountNodeFS": "NONE",
        "pyodide_js.FS": "NONE",
        "pyodide_js._module": "NONE",
        "pyodide_js._api": "NONE",
        "micropip": "ModuleNotFoundError",
        "run_js": "ImportError",  # needs js.eval, which jsglobals omits
    }
    for vector, want in expected.items():
        assert checks.get(vector) == want, f"{vector}: {checks.get(vector)!r}"
    # No WebSocket transport and no data ever received — sockets are dead
    # even though Emscripten fakes connect() success.
    assert checks["js_websocket"] == "NONE"
    assert not checks["socket_data"].startswith("GOT")


# --- kill limits --------------------------------------------------------------


def test_cpu_limit_kills_busy_loop():
    result = run_python("while True: pass", cpu_seconds=2, wall_seconds=30)
    assert not result.ok
    assert result.error == "killed: exceeded 2s CPU"


def test_memory_limit_kills_allocator():
    result = run_python(
        "chunks = []\n"
        "while True:\n"
        "    chunks.append(bytearray(10 * 1024 * 1024))\n",
        memory_budget_mb=50,
        wall_seconds=30,
    )
    assert not result.ok
    assert result.error == "killed: exceeded 50 MB memory budget"


def test_wall_clock_limit_kills_slow_job():
    # A pure sleep burns no CPU, so only the wall clock can catch it.
    result = run_python("import time; time.sleep(60)", wall_seconds=5)
    assert not result.ok
    assert result.error == "killed: exceeded 5s wall clock"


# --- assistant capability -----------------------------------------------------


def _ctx() -> AssistantActionContext:
    return AssistantActionContext(
        journal_id=None, room_uuid=uuid4(), agent_uuid=uuid4(), step_index=0
    )


def test_action_python_run_success():
    obs = _action_python_run(_ctx(), {"code": "print('hi')\n2 ** 100"})
    assert obs.ok
    assert "hi" in obs.text
    assert "1267650600228229401496703205376" in obs.text


def test_action_python_run_error():
    obs = _action_python_run(_ctx(), {"code": "1/0"})
    assert not obs.ok
    assert "ZeroDivisionError" in obs.text


def test_action_python_run_empty_code():
    obs = _action_python_run(_ctx(), {"code": "   "})
    assert not obs.ok
    assert "blocked" in obs.text
