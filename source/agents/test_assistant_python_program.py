"""python_run's program is written by a call of its own, in plain text.

The deciding call answers in JSON; a program written there is a JSON string
value, and the ones that failed to compile in production had all lost a
closing bracket to the JSON around them. The decision now carries a
`description`, and `AssistantAgent._write_python_program` has the program
written from it as plain source (see notes/assistant-design.md, python_run).
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

import agents.model_groups as mg
import db
from agents.assistant import (
    AssistantActionName,
    AssistantAgent,
    AssistantStepDecision,
    PYTHON_PROGRAM_REQUEST,
    python_program_from_text,
)
from agents.assistant_fakes import scripted_decisions
from agents.test_assistant_second_opinion import (  # noqa: F401 — fixtures
    _agent, _reply, app_ctx, room, sandbox_calls,
)


def _describe(description: str, **extra) -> AssistantStepDecision:
    return AssistantStepDecision(
        reason="compute the conversion",
        action=AssistantActionName.PYTHON_RUN,
        args={"description": description, **extra},
    )


# --- reading the program out of the answer -----------------------------------


def test_the_program_is_the_fenced_block():
    text = "```python\nimport math\nprint(math.sqrt(83838))\n```"
    assert python_program_from_text(text) == "import math\nprint(math.sqrt(83838))\n"


def test_bare_source_is_the_program():
    assert python_program_from_text("print(12 * 0.3048)") == "print(12 * 0.3048)\n"


def test_a_program_that_does_not_compile_is_refused_with_its_line():
    """The production failure, as plain text would never write it — but if a
    writer does, it is asked again rather than the sandbox being sent it."""
    with pytest.raises(ValueError, match=r"line 2: '\(' was never closed"):
        python_program_from_text("```python\nimport math\nprint(math.sqrt(83838)\n```")


def test_an_answer_with_no_program_is_refused():
    with pytest.raises(ValueError, match="no program"):
        python_program_from_text("```python\n\n```")


# --- the plain-text group call ------------------------------------------------


def _fake_member(monkeypatch, answers: list[str], seen: list[list]):
    """prepare_llm for one member that streams `answers` in turn, recording
    the messages each call was sent."""
    monkeypatch.setattr(db, "resolved_model_kwargs",
                        lambda _uuid: ("ollama", "writer-model", {}))

    class _LLM:
        callback_manager = None

        def stream_chat(self, messages):
            seen.append([m.content for m in messages])
            text = answers[len(seen) - 1]
            yield SimpleNamespace(message=SimpleNamespace(content=text))

    monkeypatch.setattr(mg, "prepare_llm", lambda *_a: _LLM())


def test_an_unusable_answer_goes_back_with_the_reason(monkeypatch):
    """The same member is told what was wrong and answers again, and the
    rejected answer stays in the trace beside the one that was used."""
    seen: list[list] = []
    _fake_member(monkeypatch, ["print(1", "```python\nprint(1)\n```"], seen)
    attempts: list = []
    usage: dict = {}

    code, text, _model_uuid = mg.text_llm_call(
        "assistant.python_program", [uuid4()], "system", "user",
        python_program_from_text, usage_out=usage, attempts_out=attempts)

    assert code == "print(1)\n"
    assert text == "```python\nprint(1)\n```"
    # The retry carried the rejected answer and why it was refused.
    assert seen[1][2] == "print(1"
    assert "does not compile" in seen[1][3]
    [rejected] = attempts
    assert rejected["outcome"] == "rejected"
    assert rejected["response"] == "print(1"
    assert usage["requested_at"]


def test_a_member_that_keeps_failing_hands_over_to_the_next(monkeypatch):
    seen: list[list] = []
    _fake_member(monkeypatch, ["print(1"] * 3 + ["print(2)"], seen)
    first, second = uuid4(), uuid4()

    code, _text, model_uuid = mg.text_llm_call(
        "assistant.python_program", [first, second], "system", "user",
        python_program_from_text)

    assert code == "print(2)\n"
    assert model_uuid == second
    assert len(seen) == 4


# --- in the loop --------------------------------------------------------------


def _writer(monkeypatch, *, answer: str | None = None, error: Exception | None = None):
    """Bind the writer's slot to one fake member and record its prompts."""
    prompts: list[tuple[str, str]] = []
    real = mg.resolve_assistant_model_uuids
    monkeypatch.setattr(
        mg, "resolve_assistant_model_uuids",
        lambda slot: (([uuid4()], "assistant.python_program")
                      if slot == mg_slot() else real(slot)))

    def fake_text_call(agent_name, models, system_prompt, user_prompt, parse,
                       *, usage_out=None, attempts_out=None, observer=None):
        prompts.append((system_prompt, user_prompt))
        if error is not None:
            raise error
        if usage_out is not None:
            usage_out.update({"input": 120, "output": 30, "ms": 800})
        return parse(answer), answer, models[0]

    monkeypatch.setattr(mg, "text_llm_call", fake_text_call)
    return prompts


def mg_slot():
    from agents.config import ASSISTANT_PYTHON_PROGRAM_UUID
    return ASSISTANT_PYTHON_PROGRAM_UUID


def _approve(monkeypatch):
    monkeypatch.setattr(
        AssistantAgent, "_second_opinion",
        lambda self, decision, *, reasoning, messages: (
            True, {"approved": True, "problems": []}))


def test_the_written_program_is_what_runs(room, monkeypatch, sandbox_calls):
    room_uuid, message_uuid = room
    prompts = _writer(monkeypatch, answer="```python\nprint(12 * 0.3048)\n```")
    _approve(monkeypatch)
    agent = _agent()
    agent._decide_next_step = scripted_decisions(
        _describe("Convert 12 feet to meters and print the result."),
        _reply("3.66 meters"))

    agent.handle(uuid4(), {"room_uuid": str(room_uuid),
                           "message_uuid": str(message_uuid)})

    assert sandbox_calls == ["print(12 * 0.3048)\n"]
    # The writer saw the request and the description, and nothing else.
    [(_system, user_prompt)] = prompts
    assert user_prompt == PYTHON_PROGRAM_REQUEST.format(
        request="how much is 12 feet?",
        description="Convert 12 feet to meters and print the result.")
    steps = db.list_assistant_steps(agent._run.uuid)
    [written] = [s for s in steps if s.action == "python_program"]
    assert written.code_driven and written.phase == "observed"
    assert written.model_response == "```python\nprint(12 * 0.3048)\n```"
    assert written.input_tokens == 120 and written.duration_ms == 800
    # The step records both what it was asked to do and what it ran.
    [run_step] = [s for s in steps if s.action == "python_run"]
    assert run_step.args == {
        "description": "Convert 12 feet to meters and print the result.",
        "code": "print(12 * 0.3048)\n"}


def test_a_program_the_decision_wrote_is_replaced(room, monkeypatch, sandbox_calls):
    """The decision may still put code beside its description; the writer's
    program is the one that runs."""
    room_uuid, message_uuid = room
    _writer(monkeypatch, answer="print(12 * 0.3048)")
    _approve(monkeypatch)
    agent = _agent()
    agent._decide_next_step = scripted_decisions(
        _describe("Convert 12 feet to meters.", code="print(12 * 0.3048"),
        _reply("3.66 meters"))

    agent.handle(uuid4(), {"room_uuid": str(room_uuid),
                           "message_uuid": str(message_uuid)})

    assert sandbox_calls == ["print(12 * 0.3048)\n"]


def test_a_failed_writer_means_no_program_runs(room, monkeypatch, sandbox_calls):
    room_uuid, message_uuid = room
    _writer(monkeypatch, error=RuntimeError("all models failed"))
    _approve(monkeypatch)
    agent = _agent()
    agent._decide_next_step = scripted_decisions(
        _describe("Convert 12 feet to meters."), _reply("giving up"))

    agent.handle(uuid4(), {"room_uuid": str(room_uuid),
                           "message_uuid": str(message_uuid)})

    assert sandbox_calls == []
    steps = db.list_assistant_steps(agent._run.uuid)
    [written] = [s for s in steps if s.action == "python_program"]
    assert written.phase == "failed" and "all models failed" in (written.error or "")
    [run_step] = [s for s in steps if s.action == "python_run"]
    assert run_step.phase == "failed"
    assert "no program was written" in (run_step.observation or {})["text"]


def test_a_decision_without_a_description_is_sent_back():
    from agents.assistant import CAPABILITIES

    agent = _agent()
    agent._caps = dict(CAPABILITIES)
    problem = agent._validate_decision(AssistantStepDecision(
        reason="compute", action=AssistantActionName.PYTHON_RUN,
        args={"code": "print(1)"}))
    assert problem is not None and "'description'" in problem


# --- a computation that already ran -------------------------------------------


def test_a_finished_computation_is_refused_inside_the_decide_call():
    """The model reran a finished square root at every step to the limit;
    refused with the output it already had, it replied with it."""
    agent = _agent()
    agent._turn_computes = {
        "compute the square root of 83338 and print it.": (0, "288.68321738542403")}
    validate = agent._repeat_read_validator()

    with pytest.raises(ValueError) as refused:
        validate(_describe("Compute the square root of 83338  and print it."))
    assert "already ran at step 1" in str(refused.value)
    assert "288.68321738542403" in str(refused.value)
    # A different computation is not a repeat — and word order is meaning.
    validate(_describe("Compute the square root of 83838 and print it."))


def test_the_refusals_are_bounded():
    agent = _agent()
    agent._turn_computes = {"add two and two.": (0, "4")}
    validate = agent._repeat_read_validator()
    for _ in range(agent.READ_REPEAT_REJECTIONS):
        with pytest.raises(ValueError):
            validate(_describe("Add two and two."))
    validate(_describe("Add two and two."))      # past the bound it stands


def test_a_repeat_past_the_refusals_gets_the_earlier_output(
        room, monkeypatch, sandbox_calls):
    """The loop answers it with what the computation printed rather than
    writing, reviewing and running the program again."""
    room_uuid, message_uuid = room
    prompts = _writer(monkeypatch, answer="print(12 * 0.3048)")
    _approve(monkeypatch)
    agent = _agent()
    agent._decide_next_step = scripted_decisions(
        _describe("Convert 12 feet to meters."),
        _describe("Convert 12 feet to meters."),
        _reply("3.6576 meters"))

    agent.handle(uuid4(), {"room_uuid": str(room_uuid),
                           "message_uuid": str(message_uuid)})

    assert sandbox_calls == ["print(12 * 0.3048)\n"]
    assert len(prompts) == 1                     # written once
    runs = [s for s in db.list_assistant_steps(agent._run.uuid)
            if s.action == "python_run"]
    assert [s.phase for s in runs] == ["observed", "observed"]
    second = (runs[1].observation or {})["text"]
    assert second.startswith("3.6576") and "was not run again" in second
