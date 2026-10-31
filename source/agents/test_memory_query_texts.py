"""memory_query searches with several texts: written by their own call after
the deciding call chose the search, merged, and scored once
(AssistantAgent._write_memory_queries, _filter_recalled_candidates)."""

from uuid import uuid4

import pytest

import db
from agents.assistant import (
    MEMORY_QUERIES_REQUEST,
    AssistantActionName,
    MemoryQueryTexts,
    _action_query_memory,
    _memory_query_texts,
    _query_overlap,
    _read_key,
)
from agents.assistant_fakes import scripted_decisions
from agents.test_assistant_actions import (  # noqa: F401 — fixtures
    _agent,
    _ctx,
    _decision,
    _seed_entries,
    _steps_for,
    _stub_seed_kb,
    _use_reranker,
    app_ctx,
    room,
)
from memory.seed_memory import Match, SeedMemory


# --- the texts ------------------------------------------------------------------------


def test_texts_come_from_queries_or_a_lone_query():
    assert _memory_query_texts({"queries": [" a shed ", "", "A SHED", "a roof",
                                            "a gate", "a fence"]}) == [
        "a shed", "a roof", "a gate"]
    assert _memory_query_texts({"query": "a shed"}) == ["a shed"]
    assert _memory_query_texts({"path": "garden.shed"}) == ["garden.shed"]
    assert _memory_query_texts({}) == []


def test_overlap_counts_only_the_longer_words():
    score, shared = _query_overlap("paint for the garden shed", "the garden gate")
    assert shared == ["garden"]
    assert score == pytest.approx(1 / 4)       # garden / paint, garden, shed, gate
    assert _query_overlap("a to of", "in an at") == (0.0, [])


def test_the_read_key_ignores_the_order_of_the_queries():
    assert (_read_key("memory_query", {"queries": ["Garden shed", "roof paint"]})
            == _read_key("memory_query", {"queries": ["paint roof", "shed garden"]}))
    assert (_read_key("memory_query", {"queries": ["garden shed"]})
            != _read_key("memory_query", {"queries": ["garden gate"]}))


# --- the search -------------------------------------------------------------------------


def test_each_text_is_searched_and_the_seeds_are_merged(app_ctx):
    searched = []

    def fake_seed(query, *, qctx, **_):
        searched.append(query)
        return [SeedMemory(uuid="id-shed", path="garden.shed", source="user-overlay",
                           answer="A wooden shed.", score=1.0)] + (
            [SeedMemory(uuid="id-gate", path="garden.gate", source="user-overlay",
                        answer="A green gate.", score=0.9)]
            if query == "garden gate" else [])

    obs = _action_query_memory(
        _ctx(), {"queries": ["garden shed", "garden gate", "tool storage"]},
        _seed_retriever=fake_seed, _path_lookup=lambda q, *, qctx: [])
    assert searched == ["garden shed", "garden gate", "tool storage"]
    assert obs.text.count("A wooden shed.") == 1
    assert "A green gate." in obs.text


def test_a_path_text_is_looked_up_and_leads_the_searched_facts(app_ctx):
    searched = []

    def fake_seed(query, *, qctx, **_):
        searched.append(query)
        return [SeedMemory(uuid="id-gate", path="garden.gate", source="user-overlay",
                           answer="A green gate.", score=0.9)]

    def fake_path(query, *, qctx):
        return ([SeedMemory(uuid="id-shed", path="garden.shed", source="user-overlay",
                            answer="A wooden shed.", score=1.0)]
                if query == "garden.shed" else [])

    obs = _action_query_memory(
        _ctx(), {"queries": ["garden.shed", "garden gate"]},
        _seed_retriever=fake_seed, _path_lookup=fake_path)
    assert searched == ["garden gate"]
    assert obs.text.index("A wooden shed.") < obs.text.index("A green gate.")
    assert obs.data["recall_filter"]["paths"] == ["garden.shed"]


def _reranker_world(monkeypatch, scores_by_query):
    """A seed fact per text plus two every text finds — five candidates, so
    the keep/drop policy competes rather than keeping all — scored by a fake
    reranker that answers from `scores_by_query`; returns the queries it was
    asked."""
    import agents.recall_filter as rf
    import agents.recall_reranker as rr
    from memory import seed_memory as qkb

    _stub_seed_kb(monkeypatch, qkb)
    _use_reranker(monkeypatch)
    entries = {f"qa-{w}": {"kind": "static", "path": f"garden.{w}",
                           "_source": "upstream", "answer": f"The {w}."}
               for w in ("shed", "gate", "pond", "path", "lawn")}
    _seed_entries(monkeypatch, qkb, entries)
    monkeypatch.setattr(rf, "get_entry", qkb.get_entry)
    monkeypatch.setattr(qkb, "_hybrid_seed_ranked", lambda q, vs, **_: [
        Match(qa_id=f"qa-{w}", method="semantic", score=0.8, matched_question=q)
        for w in (q.split()[-1], "path", "lawn")])
    monkeypatch.setattr(db, "get_agent_model_binding", lambda agent_uuid: None)
    asked = []

    def fake_rerank(query, documents, *, model, **_):
        asked.append(query)
        scores = scores_by_query[query]
        return rr.RerankResult(scores={d["id"]: scores.get(d["id"], 0.0)
                                       for d in documents},
                               service_ms=10, max_length=512, tokens={})

    monkeypatch.setattr(rr, "rerank", fake_rerank)
    return asked


QUERIES = ["garden shed", "garden gate", "garden pond"]


def test_the_merged_candidates_are_scored_once_against_the_texts_joined(
        app_ctx, monkeypatch):
    asked = _reranker_world(monkeypatch, {
        "garden shed; garden gate; garden pond": {
            "qa-shed": 0.9, "qa-gate": 0.6, "qa-pond": 0.01}})
    obs = _action_query_memory(_ctx(), {"queries": QUERIES})
    assert asked == ["garden shed; garden gate; garden pond"]
    assert "The shed." in obs.text and "The gate." in obs.text
    assert "The pond." not in obs.text
    debug = obs.data["recall_filter"]
    assert debug["query"] == "garden shed; garden gate; garden pond"
    assert debug["scoring"] == "the queries joined"
    assert len(debug["candidates"]) == 5


def test_when_the_joined_texts_keep_nothing_each_text_scores_alone(
        app_ctx, monkeypatch):
    asked = _reranker_world(monkeypatch, {
        "garden shed; garden gate; garden pond": {"qa-shed": 0.02},
        "garden shed": {"qa-shed": 0.4},
        "garden gate": {"qa-gate": 0.3},
        "garden pond": {"qa-pond": 0.01}})
    obs = _action_query_memory(_ctx(), {"queries": QUERIES})
    assert asked == ["garden shed; garden gate; garden pond", *QUERIES]
    assert "The shed." in obs.text and "The gate." in obs.text
    assert "The pond." not in obs.text
    debug = obs.data["recall_filter"]
    assert debug["scoring"] == "each query, best score"
    assert debug["service_ms"] == 40


def test_a_single_text_is_scored_as_before(app_ctx, monkeypatch):
    asked = _reranker_world(monkeypatch, {"garden shed": {"qa-shed": 0.9}})
    obs = _action_query_memory(_ctx(), {"query": "garden shed"})
    assert asked == ["garden shed"]
    assert "scoring" not in obs.data["recall_filter"]


# --- the writing call ---------------------------------------------------------------------


def _writer(monkeypatch, answers):
    """An agent whose decide slot has a model and whose calls answer from
    `answers` in turn, running each through the call's validator the way the
    real seam does. Returns the agent and the calls it saw."""
    agent = _agent()
    agent._run = None
    agent._turn_log = []
    monkeypatch.setattr(agent, "_slot_models",
                        lambda slot: ([uuid4()], uuid4(), "assistant.decide"))
    seen = []

    def fake_completion(*, response_model, followup_turns, validator=None, **_):
        assert response_model is MemoryQueryTexts
        refusals = []
        for queries in answers:
            result = MemoryQueryTexts(queries=queries)
            try:
                if validator is not None:
                    validator(result)
            except ValueError as exc:
                refusals.append(str(exc))
                continue
            seen.append({"turns": followup_turns, "refusals": refusals})
            agent._last_response_text = result.model_dump_json()
            agent._last_rejected_attempts = [{"error": r} for r in refusals]
            return result
        raise RuntimeError("every answer was refused")

    agent._structured_completion = fake_completion
    return agent, seen


def _write(agent, tried=(), args=None):
    decision = _decision(AssistantActionName.MEMORY_QUERY, **(args or {}))
    return agent._write_memory_queries(
        decision, step_index=0, system_prompt="sys", user_prompt="usr",
        followup_turns=[{"role": "user", "content": "the request"}],
        model_response='{"action": "memory_query", "args": {}}',
        tried=list(tried))


def test_the_written_texts_are_what_the_search_runs(monkeypatch):
    agent, seen = _writer(monkeypatch, [["garden shed", "roof paint", "tool storage"]])
    assert _write(agent) == {"queries": ["garden shed", "roof paint", "tool storage"]}
    turns = seen[0]["turns"]
    # The deciding conversation, continued: its turns, its answer, the request.
    assert turns[0] == {"role": "user", "content": "the request"}
    assert turns[1] == {"role": "assistant",
                        "content": '{"action": "memory_query", "args": {}}'}
    assert turns[2]["content"].startswith(MEMORY_QUERIES_REQUEST.split("{count}")[0])
    assert "No search has run this turn yet." in turns[2]["content"]
    assert agent._steps[-1]["action"] == "memory_queries"
    assert agent._steps[-1]["code_driven"]


def test_overlapping_texts_are_sent_back_naming_the_shared_words(monkeypatch):
    agent, seen = _writer(monkeypatch, [
        ["garden shed roof paint", "garden shed roof tiles", "pond"],
        ["garden shed", "roof paint", "tool storage"]])
    assert _write(agent) == {"queries": ["garden shed", "roof paint", "tool storage"]}
    (refusal,) = seen[0]["refusals"]
    assert "search texts 1 and 2 are too similar" in refusal
    assert "garden, roof, shed (60% of their words)" in refusal


def test_texts_sharing_a_name_and_half_their_words_pass(monkeypatch):
    agent, seen = _writer(monkeypatch, [
        ["garden shed paint", "garden shed roof", "pond liner"]])
    assert _write(agent) == {"queries": ["garden shed paint", "garden shed roof",
                                         "pond liner"]}
    assert seen[0]["refusals"] == []


def test_a_text_like_one_already_run_is_sent_back(monkeypatch):
    agent, seen = _writer(monkeypatch, [
        ["garden shed paint", "roof", "pond"],
        ["roof tiles", "pond liner", "tool storage"]])
    _write(agent, tried=["paint for the garden shed"])
    (refusal,) = seen[0]["refusals"]
    assert '"paint for the garden shed", which already ran' in refusal
    assert "Searches already run this turn" in seen[0]["turns"][2]["content"]


def test_too_few_texts_are_sent_back(monkeypatch):
    agent, seen = _writer(monkeypatch, [["garden shed"],
                                        ["garden shed", "roof paint", "pond"]])
    _write(agent)
    assert "write 3 different search texts; this answer has 1" in seen[0]["refusals"][0]


def test_past_the_refusals_the_texts_run_as_written(monkeypatch):
    same = ["garden shed roof paint", "garden shed roof tiles",
            "garden shed roof door"]
    agent, seen = _writer(monkeypatch, [same, same, same])
    assert _write(agent) == {"queries": same}
    assert len(seen[0]["refusals"]) == agent.MEMORY_QUERY_REWRITES


def test_without_models_the_decision_s_own_query_searches(monkeypatch):
    agent = _agent()
    monkeypatch.setattr(agent, "_slot_models", lambda slot: (None, None, None))
    assert _write(agent, args={"query": "garden shed"}) == {"query": "garden shed"}


def test_a_failed_call_falls_back_and_leaves_a_failed_row(monkeypatch):
    agent, _seen = _writer(monkeypatch, [])
    assert _write(agent, args={"query": "garden shed"}) == {"query": "garden shed"}
    assert agent._steps[-1]["phase"] == "failed"


# --- in the loop -------------------------------------------------------------------------


def test_the_step_records_the_written_texts_and_the_next_writer_sees_them(room):
    room_uuid, message_uuid = room
    agent = _agent()
    written = [["garden shed", "roof paint", "tool storage"],
               ["pond liner", "gate hinge", "fence posts"]]
    shown = []

    def fake_writer(decision, *, tried, **_):
        shown.append(list(tried))
        return {"queries": written[len(shown) - 1]}

    agent._write_memory_queries = fake_writer
    agent._decide_next_step = scripted_decisions(
        _decision(AssistantActionName.MEMORY_QUERY),
        _decision(AssistantActionName.MEMORY_QUERY),
        _decision(AssistantActionName.REPLY, message="Done."),
    )
    result = agent.handle(
        uuid4(), {"room_uuid": str(room_uuid), "message_uuid": str(message_uuid)})
    assert result["status"] == "finished"
    searches = [s for s in _steps_for(result["assistant_run_uuid"])
                if s.action == "memory_query"]
    assert [s.args for s in searches] == [{"queries": q} for q in written]
    assert shown == [[], written[0]]


def test_a_read_by_uuid_writes_no_texts(room):
    room_uuid, message_uuid = room
    agent = _agent()

    def fake_writer(*_a, **_k):
        raise AssertionError("a uuid read needs no search texts")

    agent._write_memory_queries = fake_writer
    agent._decide_next_step = scripted_decisions(
        _decision(AssistantActionName.MEMORY_QUERY, uuid=str(uuid4())),
        _decision(AssistantActionName.REPLY, message="Done."),
    )
    result = agent.handle(
        uuid4(), {"room_uuid": str(room_uuid), "message_uuid": str(message_uuid)})
    assert result["status"] == "finished"


def test_the_writing_call_lands_as_its_own_step_row(room, monkeypatch):
    """In a recorded run the writing call is a code-driven row with what a
    call's row carries — its turns, its answer, the model, the cost — and the
    read model draws it as a call of its own beside the search it wrote for."""
    from agents.config import ASSISTANT_DECIDE_UUID

    room_uuid, message_uuid = room
    agent = _agent()
    group, model = uuid4(), uuid4()
    monkeypatch.setattr(
        agent, "_slot_models",
        lambda slot: (([model], group, "assistant.decide")
                      if slot == ASSISTANT_DECIDE_UUID else (None, None, None)))
    scripted = scripted_decisions(
        _decision(AssistantActionName.MEMORY_QUERY),
        _decision(AssistantActionName.REPLY, message="Done."))

    def decide(**kwargs):
        # The prompts the real seam snapshots, which the writing call continues.
        agent._last_system_prompt, agent._last_user_prompt = "sys", "usr"
        agent._last_followup_turns = None
        decision = scripted(**kwargs)
        agent._last_response_text = decision.model_dump_json()
        return decision

    texts = ["garden shed", "roof paint", "tool storage"]

    def fake_completion(*, response_model, **_):
        assert response_model is MemoryQueryTexts
        agent._last_model_uuid = model
        agent._last_usage = {"input": 900, "output": 20, "ms": 700}
        agent._last_reasoning = None
        agent._last_response_text = MemoryQueryTexts(queries=texts).model_dump_json()
        agent._last_rejected_attempts = []
        return MemoryQueryTexts(queries=texts)

    agent._decide_next_step = decide
    agent._structured_completion = fake_completion
    result = agent.handle(
        uuid4(), {"room_uuid": str(room_uuid), "message_uuid": str(message_uuid)})
    assert result["status"] == "finished"

    steps = _steps_for(result["assistant_run_uuid"])
    row = next(s for s in steps if s.action == "memory_queries")
    assert row.code_driven and row.phase == "observed"
    assert (row.model_uuid, row.model_group_uuid) == (model, group)
    assert (row.input_tokens, row.output_tokens, row.duration_ms) == (900, 20, 700)
    assert row.followup_turns[0]["role"] == "assistant"
    assert row.followup_turns[-1]["content"].startswith("memory_query will now search")
    assert '"garden shed"' in row.model_response
    search = next(s for s in steps if s.action == "memory_query")
    assert search.args == {"queries": texts}
    assert row.step_index == search.step_index

    events = db.run_events(db.get_assistant_run(result["assistant_run_uuid"]), steps)
    assert any(e["kind"] == "llm" and e["label"] == "memory_queries" for e in events)
