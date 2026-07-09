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

    def describe_models(self):
        return []


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
    assert config.llm_timeout_s == 120.0


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


def _scope():
    from research.scope import ScopeModel

    return ScopeModel(
        meanings=["a display standard", "a physical connector"],
        chosen_scope="The display standard.",
        excluded=["the connector", "  "],
    )


def test_resolve_scope_and_block_rendering():
    from research.scope import resolve_scope, scope_block, scope_markdown

    caller = FakeCaller(structured={prompts.SCOPE_SYSTEM: [_scope()]})
    scope = resolve_scope(caller, "history of the svga port")
    assert caller.calls[0] == (prompts.SCOPE_SYSTEM, "history of the svga port")
    block = scope_block(scope)
    assert block == (
        "SCOPE: The display standard.\nOUT OF SCOPE: the connector"
    )
    assert scope_markdown(scope) == (
        "The display standard.\n\nOut of scope: the connector."
    )


def test_scope_block_with_no_exclusions():
    from research.scope import ScopeModel, scope_block, scope_markdown

    scope = ScopeModel(meanings=["m"], chosen_scope="Everything.", excluded=[])
    assert "OUT OF SCOPE: nothing noted" in scope_block(scope)
    assert scope_markdown(scope) == "Everything."


def test_generate_plan_includes_scope_block():
    caller = FakeCaller(plain={prompts.PLANNER_SYSTEM: ["plan"]})
    generate_plan(caller, "the query", "SCOPE: X\nOUT OF SCOPE: Y")
    user_prompt = caller.calls[0][1]
    assert user_prompt.startswith("the query")
    assert "SCOPE: X" in user_prompt


def test_resolve_scope_scrubs_hypothetical_framing():
    from research.scope import ScopeModel, resolve_scope

    caller = FakeCaller(
        structured={
            prompts.SCOPE_SYSTEM: [
                ScopeModel(
                    meanings=["a film"],
                    chosen_scope=(
                        "Obsession 2025 – a (hypothetical or upcoming) film "
                        "titled *Obsession 2025*"
                    ),
                    excluded=[],
                )
            ]
        }
    )
    scope = resolve_scope(caller, "analyze the Obsession 2025 movie")
    assert scope.chosen_scope == (
        "Obsession 2025 – a film titled *Obsession 2025*"
    )


def test_resolve_scope_scrubs_bare_hypothetical_word():
    from research.scope import _scrub_hypothetical

    assert _scrub_hypothetical("A hypothetical device from 1990.") == (
        "A device from 1990."
    )
    assert _scrub_hypothetical("A possibly nonexistent standard.") == (
        "A standard."
    )
    assert _scrub_hypothetical("No framing here.") == "No framing here."
