"""Killable subprocess runner for a single /model test probe.

A test probe is one blocking LLM call (test_chat / test_structured_output /
test_tool_call). Run in-process, it can't be cancelled — a runaway model just
hangs the request thread. So the web layer runs each probe here, in a throwaway
subprocess it can SIGKILL on Stop; killing the process closes its HTTP socket to
the provider, which makes the provider (e.g. Ollama) stop generating.

Protocol: read one JSON request object on stdin —
    {"action", "provider_id", "model", "arguments"}
— and write exactly one JSON result line to stdout:
    {"ok": true,  "message", "elapsed", "kind"}   on success
    {"ok": false, "error", "kind"}                on failure

stdout is reserved for that single result line: the test's own output and any
library chatter is redirected to stderr (which the parent discards) so it can't
corrupt the result the parent parses.
"""

import json
import os
import sys


def main() -> None:
    req = json.load(sys.stdin)
    action = req.get("action")

    # Reserve fd 1 for the result, then point stdout (fd 1) at stderr so nothing
    # the test or its libraries prints can land on the channel the parent reads.
    result_fd = os.dup(1)
    os.dup2(2, 1)

    try:
        import llm

        res = llm.run_named_test(
            action, req["provider_id"], req["model"], req["arguments"]
        )
        result = {"ok": True, "kind": action, **res}
    except Exception as e:  # noqa: BLE001 — any failure becomes a rendered error
        result = {"ok": False, "error": f"{type(e).__name__}: {e}", "kind": action}

    os.write(result_fd, (json.dumps(result) + "\n").encode())


if __name__ == "__main__":
    main()
