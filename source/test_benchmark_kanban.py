"""Tests for the deterministic parts of benchmark_kanban.py (no LLM): board
generation, instruction/expected validity, serialization id-coverage via the
PRODUCTION renderers, and the grader. The LLM-facing run() paths are exercised
manually / from /benchmark against live local models.
"""

import json
import random

import pytest

from benchmark_kanban import (
    _OPS,
    grade,
    make_board,
    make_instruction,
    serialize_board,
)


@pytest.fixture
def rng():
    return random.Random(42)


def test_make_board_is_well_formed(rng):
    data, agent_names = make_board(rng)
    col_uuids = [c["uuid"] for c in data["columns"]]
    task_uuids = [t["uuid"] for t in data["tasks"]]
    titles = [t["title"] for t in data["tasks"]]
    all_ids = [data["uuid"], *col_uuids, *task_uuids, *agent_names]
    assert len(set(all_ids)) == len(all_ids)          # globally unique ids
    assert len(set(titles)) == len(titles)            # titles are referenceable
    assert [c["name"] for c in data["columns"]] == ["To do", "In progress", "Done"]
    assert all(t["columnUuid"] in col_uuids for t in data["tasks"])
    # Every agent is assigned somewhere, so a claim instruction's agentId is
    # readable off the serialization.
    assigned = {t["agentUuid"] for t in data["tasks"] if t["agentUuid"]}
    assert assigned == set(agent_names)


@pytest.mark.parametrize("fmt", ["markdown", "json"])
def test_serialization_contains_every_referencable_id(rng, fmt):
    """The benchmark is only fair if the model CAN read the answer out of the
    context: every task/column/agent id must appear in both formats (rendered
    by the production renderers, not a copy). In markdown, agent names render
    inline-ESCAPED (agent_red → agent\\_red — same as production role names
    like workspace_shell), which is part of what the benchmark measures."""
    from db.kanban import _md_inline

    data, agent_names = make_board(rng)
    text = serialize_board(data, agent_names, fmt)
    for c in data["columns"]:
        assert c["uuid"] in text and c["name"] in text
    for t in data["tasks"]:
        assert t["uuid"] in text and t["title"] in text
    for agent_uuid, name in agent_names.items():
        assert agent_uuid in text
        assert (name if fmt == "json" else _md_inline(name)) in text
    if fmt == "json":
        json.loads(text)  # valid JSON document


@pytest.mark.parametrize("kind", _OPS)
def test_make_instruction_expected_is_answerable(rng, kind):
    """The expected operation references ids that exist on the board, the
    instruction names the task title, and the target task is never in Done
    (the board's last column — not runnable)."""
    for _ in range(20):  # several random boards per kind
        data, agent_names = make_board(rng)
        instruction, expected = make_instruction(rng, data, agent_names, kind)
        task = next(t for t in data["tasks"] if t["uuid"] == expected["taskId"])
        assert task["title"] in instruction
        assert task["columnUuid"] != data["columns"][-1]["uuid"]
        if kind == "move":
            target = next(c for c in data["columns"]
                          if c["uuid"] == expected["columnId"])
            assert target["name"] in instruction
            assert expected["columnId"] != task["columnUuid"]  # a real move
        if kind == "claim":
            assert expected["agentId"] in agent_names
            assert agent_names[expected["agentId"]] in instruction
        if kind in ("complete_ok", "complete_failed"):
            assert expected["ok"] is (kind == "complete_ok")
            assert expected["op"] == "complete"


def test_make_instruction_unknown_kind(rng):
    data, agent_names = make_board(rng)
    with pytest.raises(ValueError):
        make_instruction(rng, data, agent_names, "nope")


def test_grade():
    expected = {"op": "move", "taskId": "t-1", "columnId": "c-2"}
    assert grade(expected, {"op": "move", "taskId": "t-1", "columnId": "c-2"})
    # Extraneous fields (a small model not zeroing irrelevant ones) are fine.
    assert grade(expected, {"op": "move", "taskId": "t-1", "columnId": "c-2",
                            "agentId": "junk", "ok": True})
    # Wrong op, wrong/missing id, or no response at all are not.
    assert not grade(expected, {"op": "claim", "taskId": "t-1", "columnId": "c-2"})
    assert not grade(expected, {"op": "move", "taskId": "t-9", "columnId": "c-2"})
    assert not grade(expected, {"op": "move", "taskId": "t-1"})
    assert not grade(expected, None)
    # Booleans must match exactly (complete ok=false is not ok=true).
    assert not grade({"op": "complete", "taskId": "t", "ok": False},
                     {"op": "complete", "taskId": "t", "ok": True})


def test_benchmark_specs_registered():
    """The 2×2 decision matrix lives in its OWN spec set (its own page), not
    in the general /benchmark suite."""
    from benchmark_runner import BENCHMARK_SPECS, KANBAN_BENCHMARK_SPECS, SPEC_SETS

    names = [name for name, _cls, _kw in KANBAN_BENCHMARK_SPECS]
    assert names == ["kanban_md_struct", "kanban_json_struct",
                     "kanban_md_tools", "kanban_json_tools"]
    kw = {name: kwargs for name, _cls, kwargs in KANBAN_BENCHMARK_SPECS}
    assert kw["kanban_md_struct"]["context_format"] == "markdown"
    assert kw["kanban_json_tools"]["context_format"] == "json"
    assert not any(n.startswith("kanban") for n, _c, _k in BENCHMARK_SPECS)
    assert SPEC_SETS["kanban"] is KANBAN_BENCHMARK_SPECS


def test_collect_targets_spec_set_aware(monkeypatch):
    """The kanban matrix compares structured output AGAINST function calling,
    so its runner must not pre-filter targets to structured-capable overrides
    (that would bias the tools columns toward the structured-capable set).
    The general suite keeps the structured-only filter."""
    from types import SimpleNamespace
    from uuid import uuid4

    import benchmark_runner as br

    struct_only = SimpleNamespace(uuid=uuid4(), effective_display_name="struct only")
    tools_only = SimpleNamespace(uuid=uuid4(), effective_display_name="tools only")
    both = SimpleNamespace(uuid=uuid4(), effective_display_name="both")
    cfg = SimpleNamespace(available=True, provider="lmstudio", model_name="m",
                          effective_display_name="m")
    monkeypatch.setattr(br.db, "list_model_configs_with_overrides",
                        lambda: [(cfg, [struct_only, tools_only, both])])
    struct_set = {struct_only.uuid, both.uuid}
    tool_set = {tools_only.uuid, both.uuid}
    monkeypatch.setattr(br.db, "member_uses_structured_output",
                        lambda u: u in struct_set)
    monkeypatch.setattr(br.db, "member_is_function_calling",
                        lambda u: u in tool_set)

    general = [t["uuid"] for t in br.BenchmarkRunner("general")._collect_targets()]
    kanban = [t["uuid"] for t in br.BenchmarkRunner("kanban")._collect_targets()]
    assert general == [str(struct_only.uuid), str(both.uuid)]
    assert kanban == [str(struct_only.uuid), str(tools_only.uuid), str(both.uuid)]


def test_benchmark_kanban_page_renders():
    """/benchmark_kanban renders the shared suite page over the kanban spec
    set, with its own state/start/stop endpoints; /benchmark_basic keeps the
    general specs only."""
    from webapp.core import app

    client = app.test_client()
    body = client.get("/benchmark_kanban").get_data(as_text=True)
    assert "Benchmark kanban" in body
    for name in ("kanban_md_struct", "kanban_json_struct",
                 "kanban_md_tools", "kanban_json_tools"):
        assert name in body
    assert "/benchmark_kanban/state" in body
    assert "/benchmark_kanban/start" in body
    assert client.get("/benchmark_kanban/state").status_code == 200
    general = client.get("/benchmark_basic").get_data(as_text=True)
    assert "kanban_md_struct" not in general
    assert "base64_decode" in general
