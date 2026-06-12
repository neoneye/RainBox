"""Pure unit tests for lm_studio.py helpers — no HTTP, no subprocess.

The full ensure_loaded() integration requires a running LM Studio; that
is exercised by hand via the chat UI (the MCP agent calls ensure_loaded
before each LLM construction).
"""

from providers.lm_studio import _max_loaded_context, find_instances


# Sample matching the real /api/v0/models response shape.
_SAMPLE = [
    {
        "id": "ibm/granite-4-h-tiny",
        "state": "loaded",
        "loaded_context_length": 4096,
        "max_context_length": 1048576,
    },
    {
        "id": "ibm/granite-4-h-tiny:2",
        "state": "loaded",
        "loaded_context_length": 8192,
        "max_context_length": 1048576,
    },
    {
        "id": "hermes-2-pro-mistral-7b",
        "state": "not-loaded",
        "max_context_length": 32768,
    },
    {
        "id": "microsoft/phi-4-mini-reasoning",
        "state": "loaded",
        "loaded_context_length": 4096,
    },
]


def test_find_instances_matches_bare_and_suffixed():
    found = find_instances("ibm/granite-4-h-tiny", _SAMPLE)
    ids = {m["id"] for m in found}
    assert ids == {"ibm/granite-4-h-tiny", "ibm/granite-4-h-tiny:2"}


def test_find_instances_does_not_match_unrelated_prefix():
    # "phi-4-mini-reasoning" starts the same as "phi-4-mini" but we shouldn't
    # confuse a longer model name as a `:N` suffix of a shorter one.
    found = find_instances("microsoft/phi-4-mini", _SAMPLE)
    assert found == []


def test_find_instances_returns_empty_for_unknown():
    assert find_instances("does/not-exist", _SAMPLE) == []


def test_max_loaded_context_picks_largest():
    # Two granite instances loaded at 4096 and 8192 → 8192 wins.
    insts = find_instances("ibm/granite-4-h-tiny", _SAMPLE)
    assert _max_loaded_context(insts) == 8192


def test_max_loaded_context_ignores_not_loaded():
    insts = find_instances("hermes-2-pro-mistral-7b", _SAMPLE)
    assert _max_loaded_context(insts) == 0


def test_max_loaded_context_zero_when_no_instances():
    assert _max_loaded_context([]) == 0
