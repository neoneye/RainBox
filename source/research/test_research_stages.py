import pytest
from pydantic import BaseModel

from research import prompts
from research.config import ResearchConfig
from research.planner import generate_plan
from research.splitter import Subtask, SubtaskListModel, SubtaskModel, split_plan


class FakeCaller:
    """Canned responses keyed by system prompt; each key holds a FIFO queue.
    Records every call so tests can assert what went into user messages."""

    def __init__(self, structured=None, plain=None):
        self.structured_queues = {k: list(v) for k, v in (structured or {}).items()}
        self.plain_queues = {k: list(v) for k, v in (plain or {}).items()}
        self.calls: list[tuple[str, str]] = []  # (system_prompt, user_prompt)

    def structured(self, system_prompt, user_prompt, response_model) -> BaseModel:
        self.calls.append((system_prompt, user_prompt))
        return self.structured_queues[system_prompt].pop(0)

    def plain(self, system_prompt, user_prompt) -> str:
        self.calls.append((system_prompt, user_prompt))
        return self.plain_queues[system_prompt].pop(0)


def test_config_defaults():
    config = ResearchConfig()
    assert config.model_group == "research"
    assert config.search_provider == "auto"
    assert config.fetcher == "plain"
    assert config.max_subtasks == 5
    assert config.queries_per_subtask == 3
    assert config.results_per_query == 5
    assert config.fetch_per_subtask == 4
    assert config.per_source_char_cap == 8000


def test_generate_plan_passes_query_as_user_message():
    caller = FakeCaller(plain={prompts.PLANNER_SYSTEM: ["1. dig deep"]})
    plan = generate_plan(caller, "how do tides work?")
    assert plan == "1. dig deep"
    system_prompt, user_prompt = caller.calls[0]
    assert system_prompt == prompts.PLANNER_SYSTEM
    assert user_prompt == "how do tides work?"
    assert "tides" not in system_prompt


def test_generate_plan_empty_raises():
    caller = FakeCaller(plain={prompts.PLANNER_SYSTEM: ["   "]})
    with pytest.raises(RuntimeError, match="empty plan"):
        generate_plan(caller, "q")


def _subtask_list(n):
    return SubtaskListModel(
        subtasks=[
            SubtaskModel(title=f"topic {i}", description=f"study topic {i}")
            for i in range(n)
        ]
    )


def test_split_plan_assigns_ids_in_python():
    caller = FakeCaller(structured={prompts.SPLITTER_SYSTEM: [_subtask_list(3)]})
    subtasks = split_plan(caller, "the plan", max_subtasks=5)
    assert [s.id for s in subtasks] == ["S1", "S2", "S3"]
    assert subtasks[0] == Subtask(id="S1", title="topic 0", description="study topic 0")


def test_split_plan_truncates_to_max_subtasks():
    caller = FakeCaller(structured={prompts.SPLITTER_SYSTEM: [_subtask_list(8)]})
    subtasks = split_plan(caller, "the plan", max_subtasks=5)
    assert len(subtasks) == 5


def test_split_plan_drops_blank_rows_and_raises_when_none_left():
    blank = SubtaskListModel(subtasks=[SubtaskModel(title=" ", description=" ")])
    caller = FakeCaller(structured={prompts.SPLITTER_SYSTEM: [blank]})
    with pytest.raises(RuntimeError, match="no subtasks"):
        split_plan(caller, "the plan", max_subtasks=5)
