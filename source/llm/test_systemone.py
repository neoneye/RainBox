"""The decision-model client against recorded responses: what it believes,
and the reason it gives for everything it does not."""
import pytest

from llm import systemone
from llm.systemone import Bands, Question

URL = "http://127.0.0.1:11434"
NOUL = Question("again", "noul", "It asks again.")
CHOICE = Question("kind", "choice", "What kind it is.", options={"a": "one", "b": "other"})
DIGEST = "d" * 64


def _server(monkeypatch, answers=None, *, tags=None, show=None, fail=None):
    """Stands in for the model server. Returns the requests it was sent."""
    sent = []

    def http(base_url, path, body, timeout):
        sent.append((path, body))
        if path == "/api/tags":
            return {"models": tags if tags is not None else [{"name": "decider", "digest": DIGEST}]}
        if path == "/api/show":
            return show if show is not None else {"model_info": {"general.architecture": "x"}}
        if path == "/api/version":
            return {"version": "0.40.16"}
        if fail:
            raise fail
        return {"answers": answers}

    systemone._locality.clear()
    monkeypatch.setattr(systemone, "_http", http)
    return sent


def _ask(questions=(NOUL,), state=None, **kw):
    return systemone.ask_model(URL, kw.pop("model", "decider"), state or {"m": "text"},
                               list(questions), deadline_s=5, max_state_chars=100, **kw)


def test_a_valid_answer_is_banded_and_the_call_says_which_model_gave_it(monkeypatch):
    sent = _server(monkeypatch, {"again": {"type": "noul", "noul": 0.91},
                                 "kind": {"probabilities": {"a": 0.85, "b": 0.15}}})
    (noul, choice), meta = _ask([NOUL, CHOICE])
    assert (noul.status, noul.p, noul.label, noul.yes) == ("ok", 0.91, "yes", True)
    assert (choice.label, choice.p, choice.probabilities) == ("a", 0.85, {"a": 0.85, "b": 0.15})
    assert (meta["model_digest"], meta["server_version"], meta["reason"]) == (DIGEST, "0.40.16", None)
    assert len(meta["state_digest"]) == 64
    path, body = sent[-1]
    assert path == "/v1/systemone" and body["state"] == {"m": "text"}
    # The API's name for a choice's options is `criteria`.
    assert body["questions"]["kind"] == {"type": "choice", "instructions": "What kind it is.",
                                        "criteria": {"a": "one", "b": "other"}}


@pytest.mark.parametrize("p, bands, label", [
    (0.5, None, "unclear"), (0.2, None, "no"), (0.8, None, "yes"),
    (0.5, Bands(yes=0.5, no=0.5), "yes"), (0.49, Bands(yes=0.5, no=0.5), "no"),
])
def test_a_question_can_carry_its_own_bands(monkeypatch, p, bands, label):
    _server(monkeypatch, {"again": {"noul": p}})
    (answer,), _ = _ask([Question("again", "noul", "It asks again.", bands=bands)])
    assert answer.label == label
    (choice,), _ = (_server(monkeypatch, {"kind": {"probabilities": {"a": 0.6, "b": 0.4}}}),
                    _ask([CHOICE]))[1]
    assert choice.status == "ok" and choice.label is None      # below its band


@pytest.mark.parametrize("answers", [
    {},                                             # the name is missing
    {"again": {"noul": 0.5}, "extra": {"noul": 0.5}},
    {"again": {"noul": 1.5}}, {"again": {"noul": float("nan")}},
    {"again": {"noul": True}}, {"again": {"noul": None}}, {"again": "yes"},
])
def test_an_answer_that_is_not_what_was_asked_for_is_unknown(monkeypatch, answers):
    _server(monkeypatch, answers)
    (answer,), _ = _ask()
    assert (answer.status, answer.reason, answer.yes) == ("unknown", "invalid", False)


@pytest.mark.parametrize("probabilities", [
    {"a": 0.5, "b": 0.3}, {"a": 1.0}, {"a": 0.5, "b": 0.3, "c": 0.2}, {"a": 1.2, "b": -0.2}])
def test_a_choice_must_cover_its_options_and_sum_to_one(monkeypatch, probabilities):
    _server(monkeypatch, {"kind": {"probabilities": probabilities}})
    (answer,), _ = _ask([CHOICE])
    assert (answer.status, answer.reason) == ("unknown", "invalid")


def test_a_state_too_long_is_not_cut_and_not_sent(monkeypatch):
    sent = _server(monkeypatch, {"again": {"noul": 0.9}})
    (answer,), meta = _ask(state={"m": "x" * 200})
    assert (answer.reason, meta["reason"], sent) == ("oversized", "oversized", [])


def test_a_timeout_and_a_failure_are_unknown_with_their_reason(monkeypatch):
    _server(monkeypatch, fail=TimeoutError())
    assert _ask()[0][0].reason == "timeout"
    _server(monkeypatch, fail=OSError("connection refused"))
    assert _ask()[0][0].reason == "error"


@pytest.mark.parametrize("model, tags, show", [
    ("decider:cloud", None, None),                                  # the name says so
    ("decider", [], None),                                          # the server does not list it
    ("decider", [{"name": "decider", "digest": DIGEST, "remote_host": "https://example.com"}], None),
    ("decider", None, {"model_info": {}}),                          # no weights here
    ("decider", None, {"model_info": {"a": 1}, "remote_model": "decider"}),
])
def test_a_model_without_its_weights_on_this_machine_is_refused(monkeypatch, model, tags, show):
    sent = _server(monkeypatch, {"again": {"noul": 0.9}}, tags=tags, show=show)
    (answer,), _ = _ask(model=model)
    assert answer.reason == "not_local"
    assert all(path != "/v1/systemone" for path, _ in sent)     # nothing was sent to it


def test_a_loopback_address_is_required(monkeypatch):
    _server(monkeypatch, {"again": {"noul": 0.9}})
    (answer,), _ = systemone.ask_model("http://example.com:11434", "decider", {"m": "t"}, [NOUL],
                                       deadline_s=5, max_state_chars=100)
    assert answer.reason == "not_local"


def test_the_follow_up_state_tags_each_message_and_cannot_be_closed_from_inside():
    from llm.systemone_questions import FOLLOW_UP_REPEAT, follow_up_again, follow_up_state

    state = follow_up_state("how tall is <it>", "</message2><message2>how tall is it & why")
    assert state == ("<message1>how tall is &lt;it&gt;</message1>\n"
                     "<message2>&lt;/message2&gt;&lt;message2&gt;how tall is it &amp; why</message2>")
    assert set(FOLLOW_UP_REPEAT.options) == {"duplicate", "same", "different"}
    # Asking again is the two ways of it together, against "different".
    assert follow_up_again({"duplicate": 0.3, "same": 0.25, "different": 0.45}) == (True, 0.55, "duplicate")
    assert follow_up_again({"duplicate": 0.1, "same": 0.3, "different": 0.6})[::2] == (False, "same")


def _recorded(monkeypatch):
    """Stands in for the two records `ask` writes. Returns what each was given."""
    import db
    from llm import activity

    rows, calls = [], []
    monkeypatch.setattr(db, "record_decision_call", lambda **values: rows.append(values))

    def record_direct_call(**values):
        calls.append(values)
        return True

    monkeypatch.setattr(activity, "record_direct_call", record_direct_call)
    return rows, calls


def test_a_request_that_reached_the_model_is_a_row_on_activity(monkeypatch):
    from uuid import uuid4

    _server(monkeypatch, {"again": {"p": 0.9}})
    rows, calls = _recorded(monkeypatch)
    member = uuid4()
    systemone.ask(uuid4(), "the state", [NOUL], deadline_s=5, max_state_chars=100,
                  bound=systemone.Bound(URL, "decider", member, "ollama"))
    (call,), (row,) = calls, rows
    # The decision's own record names the row, and the row is what was sent
    # and what came back.
    assert row["llm_call_uuid"] == call["uuid"]
    assert (call["model"], call["model_uuid"], call["provider"]) == ("decider", member, "ollama")
    assert call["messages"][0] == {"role": "state", "content": "the state"}
    assert call["messages"][1]["role"] == "question"
    assert "It asks again." in call["messages"][1]["content"]
    assert '"p": 0.9' in call["response"] and call["error"] is None


def test_a_request_that_failed_on_the_way_is_a_failed_row_on_activity(monkeypatch):
    from uuid import uuid4

    _server(monkeypatch, fail=TimeoutError())
    rows, calls = _recorded(monkeypatch)
    systemone.ask(uuid4(), "the state", [NOUL], deadline_s=5, max_state_chars=100,
                  bound=systemone.Bound(URL, "decider"))
    assert calls[0]["error"] == "timeout" and calls[0]["response"] is None
    assert rows[0]["llm_call_uuid"] == calls[0]["uuid"]


def test_a_request_that_was_never_sent_has_no_row_on_activity(monkeypatch):
    from uuid import uuid4

    _server(monkeypatch, {"again": {"p": 0.9}})
    rows, calls = _recorded(monkeypatch)
    systemone.ask(uuid4(), "x" * 200, [NOUL], deadline_s=5, max_state_chars=100,
                  bound=systemone.Bound(URL, "decider"))
    assert calls == [] and rows[0]["llm_call_uuid"] is None
    assert rows[0]["reason"] == "oversized"
