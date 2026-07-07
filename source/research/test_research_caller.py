from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import BaseModel

import db
import llm
from research.caller import ModelCaller


class Answer(BaseModel):
    text: str


GROUP_UUID = uuid4()
MODEL_A = uuid4()
MODEL_B = uuid4()


def _patch_group(monkeypatch, members):
    group = SimpleNamespace(uuid=GROUP_UUID, name="research")
    monkeypatch.setattr(db, "list_model_groups", lambda: [group])
    monkeypatch.setattr(
        db, "get_model_group_member_uuids", lambda group_uuid: list(members)
    )
    monkeypatch.setattr(
        db,
        "resolved_model_kwargs",
        lambda model_uuid: ("ollama", f"model-{model_uuid}", {}),
    )


class FakeStructuredLLM:
    def __init__(self, raw):
        self._raw = raw

    def stream_chat(self, messages):
        yield SimpleNamespace(raw=self._raw)


class FakeLLM:
    def __init__(self, *, raw=None, reply="", fail=False):
        self._raw = raw
        self._reply = reply
        self._fail = fail

    def as_structured_llm(self, response_model):
        if self._fail:
            raise RuntimeError("model down")
        return FakeStructuredLLM(self._raw)

    def chat(self, messages):
        if self._fail:
            raise RuntimeError("model down")
        return SimpleNamespace(
            message=SimpleNamespace(content=self._reply)
        )


def test_unknown_group_lists_available(monkeypatch):
    _patch_group(monkeypatch, [MODEL_A])
    with pytest.raises(RuntimeError, match="research"):
        ModelCaller("nonexistent-group")


def test_empty_group_raises(monkeypatch):
    _patch_group(monkeypatch, [])
    with pytest.raises(RuntimeError, match="no members"):
        ModelCaller("research")


def test_structured_returns_parsed_model(monkeypatch):
    _patch_group(monkeypatch, [MODEL_A])
    monkeypatch.setattr(
        llm, "prepare_llm", lambda p, m, a: FakeLLM(raw=Answer(text="ok"))
    )
    result = ModelCaller("research").structured("sys", "user", Answer)
    assert isinstance(result, Answer) and result.text == "ok"


def test_structured_falls_back_to_next_member(monkeypatch):
    _patch_group(monkeypatch, [MODEL_A, MODEL_B])
    llms = {
        f"model-{MODEL_A}": FakeLLM(fail=True),
        f"model-{MODEL_B}": FakeLLM(raw=Answer(text="fallback")),
    }
    monkeypatch.setattr(llm, "prepare_llm", lambda p, m, a: llms[m])
    result = ModelCaller("research").structured("sys", "user", Answer)
    assert isinstance(result, Answer) and result.text == "fallback"


def test_all_members_fail_raises(monkeypatch):
    _patch_group(monkeypatch, [MODEL_A, MODEL_B])
    monkeypatch.setattr(llm, "prepare_llm", lambda p, m, a: FakeLLM(fail=True))
    with pytest.raises(RuntimeError, match="all models"):
        ModelCaller("research").structured("sys", "user", Answer)


def test_plain_returns_text(monkeypatch):
    _patch_group(monkeypatch, [MODEL_A])
    monkeypatch.setattr(
        llm, "prepare_llm", lambda p, m, a: FakeLLM(reply="  hello  ")
    )
    assert ModelCaller("research").plain("sys", "user") == "hello"


def test_plain_empty_reply_falls_through(monkeypatch):
    _patch_group(monkeypatch, [MODEL_A, MODEL_B])
    llms = {
        f"model-{MODEL_A}": FakeLLM(reply=""),
        f"model-{MODEL_B}": FakeLLM(reply="second"),
    }
    monkeypatch.setattr(llm, "prepare_llm", lambda p, m, a: llms[m])
    assert ModelCaller("research").plain("sys", "user") == "second"


def test_group_resolvable_by_uuid(monkeypatch):
    _patch_group(monkeypatch, [MODEL_A])
    monkeypatch.setattr(
        llm, "prepare_llm", lambda p, m, a: FakeLLM(reply="via uuid")
    )
    assert ModelCaller(str(GROUP_UUID)).plain("sys", "user") == "via uuid"
