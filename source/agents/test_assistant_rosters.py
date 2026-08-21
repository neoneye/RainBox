"""The measured failure, end to end.

"who are all my X" has an N-entry answer set, and `memory_query`'s candidate
budget is a fixed top-k, so members past k never reach the model. Against the
live registry, six sibling entries under one path prefix produced two
candidates and an answer built from those two.

A derived roster collapses those N entries into one, so a single candidate slot
carries the whole set. These tests drive the real route —
`_action_query_memory` — with a genuinely synthesised roster, and assert that
all six members reach the observation. Ranking and filtering use the existing
seams; no model and no embedder run.

Fixtures are synthetic. The operator's own relations live only under
`customize.dir`.
"""
import json
from uuid import uuid4

import pytest

import db
import memory.seed_memory as kb
from agents.assistant import AssistantActionContext, _action_query_memory

PREFIX = "human.subject.friend"
QUESTION = "who are my friends"
NAMES = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    try:
        yield app
    finally:
        db.db.session.rollback()
        ctx.pop()


def _ctx() -> AssistantActionContext:
    return AssistantActionContext(
        journal_id=uuid4(), room_uuid=uuid4(), agent_uuid=uuid4(), step_index=0
    )


@pytest.fixture
def registry(tmp_path, monkeypatch):
    """Six members under one declared prefix, loaded through the real loader so
    the roster under test is the one synthesis actually produces."""
    base = tmp_path / "question_answer.jsonl"
    base.write_text("")
    customize = tmp_path / "customize"
    customize.mkdir()
    overlay = customize / "question_answer.jsonl"
    overlay.write_text("".join(
        json.dumps({"id": f"m-{n}", "path": f"{PREFIX}.{n}", "kind": "static",
                    "questions": [f"who is {n}"], "answer": f"About {n}.",
                    "label": n.capitalize()}) + "\n"
        for n in NAMES))
    (customize / "relations.json").write_text(json.dumps({"relations": [{
        "prefix": PREFIX, "title": "friends", "complete": False,
        "shield": None, "questions": [QUESTION]}]}))

    monkeypatch.setattr(kb, "QA_JSONL_PATH", base)
    monkeypatch.setattr(kb, "_overlay_path", lambda: overlay)
    monkeypatch.setattr(kb, "_entries_by_id", {})
    monkeypatch.setattr(kb, "_alias_table", {})
    monkeypatch.setattr(kb, "_fulltext_index_cache", None)
    kb._load_kb()
    return kb.get_entry(kb._roster_id(PREFIX))


def _assert_all_six(text):
    for n in NAMES:
        assert n.capitalize() in text, f"{n} missing from the observation:\n{text}"
    assert "friends (6)" in text


def test_the_roster_lists_all_six_members(registry):
    # The premise: one entry now carries what six entries carried before.
    _assert_all_six(registry["answer"])
    assert registry["_derived"] == "roster"


def test_measured_route_returns_every_member(registry, app_ctx, monkeypatch):
    """The failing case: one candidate slot, six members in the answer.

    The roster is retrieved for real from the loaded registry; only ranking is
    faked, standing in for the vector/full-text pass that would surface it.
    """
    rid = kb._roster_id(PREFIX)
    monkeypatch.setattr(
        kb, "_semantic_ranked",
        lambda q, vs, **_: [kb.Match(qa_id=rid, method="semantic", score=0.9)])
    monkeypatch.setattr(kb, "_vector_store", lambda: object())
    monkeypatch.setattr(kb, "_ensure_populated", lambda vs: None)
    # Force the recall-filter path to fall back, exercising retrieve_seed_answers.
    monkeypatch.setattr("agents.assistant._filter_recalled_candidates",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no filter")))

    obs = _action_query_memory(_ctx(), {"query": QUESTION})
    assert obs.ok
    _assert_all_six(obs.text)


def _seed(roster):
    from memory.seed_memory import SeedMemory
    return SeedMemory(uuid=roster["id"], path=roster["path"],
                      source=roster["_source"], answer=roster["answer"],
                      score=0.9, kind="static")


def test_measured_route_through_the_recall_filter(registry, app_ctx, monkeypatch):
    """The primary path: the recall filter keeps the roster and every member
    survives into the observation.

    `_seed_retriever` is deliberately NOT supplied — passing it is the hermetic
    shortcut that skips `_filter_recalled_candidates` entirely, so a test using
    it proves nothing about this route. The filter itself is faked, and the
    fake asserts it was actually reached.
    """
    called = []

    def fake_filter(query, **kwargs):
        called.append(query)
        return [_seed(registry)], [], {"mode": "llm"}

    monkeypatch.setattr("agents.assistant._filter_recalled_candidates", fake_filter)
    monkeypatch.setattr(kb, "_vector_store", lambda: object())
    monkeypatch.setattr(kb, "_ensure_populated", lambda vs: None)

    obs = _action_query_memory(_ctx(), {"query": QUESTION})
    assert called == [QUESTION], "the recall filter was never reached"
    assert obs.ok
    _assert_all_six(obs.text)
    # Overlay provenance: the roster tiers with the operator's own facts.
    assert "user-overlay" in obs.text


def test_injected_seed_path_renders_the_roster(registry, app_ctx):
    """The hermetic `_seed_retriever` seam, named for what it is: it bypasses
    the recall filter, so it covers rendering only."""
    obs = _action_query_memory(_ctx(), {"query": QUESTION},
                               _seed_retriever=lambda q, *, qctx, **_: [_seed(registry)])
    assert obs.ok
    _assert_all_six(obs.text)


def test_every_printed_member_id_is_readable_in_full(registry, app_ctx, monkeypatch):
    """The roster is an index card, not a display list: each id it prints must
    resolve through memory_query's uuid mode."""
    printed = [line.split("[")[1].rstrip("]")
               for line in registry["answer"].split("\n")[1:]]
    assert len(printed) == 6

    monkeypatch.setattr(kb, "_vector_store", lambda: object())
    monkeypatch.setattr(kb, "_ensure_populated", lambda vs: None)
    for qa_id in printed:
        obs = _action_query_memory(_ctx(), {"uuid": qa_id})
        assert obs.ok, f"{qa_id} did not resolve"
        assert qa_id.removeprefix("m-").capitalize() in obs.text or \
            f"About {qa_id.removeprefix('m-')}." in obs.text


def test_a_member_name_query_does_not_return_the_roster(registry):
    """The roster's answer holds every label, so indexing it would make the
    roster compete with the person on a single-person question."""
    ranked = kb._fulltext_ranked("who is charlie", unlocked_shields=set())
    assert ranked, "the member itself should still rank"
    assert ranked[0].qa_id == "m-charlie"
    assert kb._roster_id(PREFIX) not in {m.qa_id for m in ranked}
