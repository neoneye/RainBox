"""Shield primitives: the pure lock predicate + candidate filter, and the
qa.unlocked_shields registry setting. Neutral placeholder shield names only."""
import db
import memory.seed_memory as kb
from memory.seed_memory import Match


def test_entry_with_no_shield_is_never_locked():
    assert kb._entry_locked({"id": "a"}, set()) is False
    assert kb._entry_locked({"id": "a", "shield": ""}, set()) is False


def test_entry_locked_when_shield_not_unlocked():
    assert kb._entry_locked({"id": "a", "shield": "alice.travel"}, set()) is True
    assert kb._entry_locked({"id": "a", "shield": "alice.travel"}, {"bob.notes"}) is True


def test_entry_unlocked_when_shield_present_in_set():
    assert kb._entry_locked({"id": "a", "shield": "alice.travel"}, {"alice.travel"}) is False


def test_drop_locked_removes_locked_and_preserves_order(monkeypatch):
    entries = {
        "u1": {"id": "u1", "shield": "alice.travel"},
        "u2": {"id": "u2"},                       # unshielded
        "u3": {"id": "u3", "shield": "bob.notes"},
    }
    monkeypatch.setattr(kb, "_entries_by_id", entries)
    matches = [Match(qa_id="u1", method="semantic", score=0.9),
               Match(qa_id="u2", method="semantic", score=0.8),
               Match(qa_id="u3", method="semantic", score=0.7)]
    kept = kb._drop_locked(matches, {"bob.notes"})   # only bob.notes unlocked
    assert [m.qa_id for m in kept] == ["u2", "u3"]    # u1 locked out, order kept


def test_unlocked_shields_setting_defaults_to_empty_list():
    app = db.make_app()
    with app.app_context():
        assert db.get_setting("qa.unlocked_shields") == []


def test_unlocked_shields_helper_reads_setting():
    app = db.make_app()
    with app.app_context():
        assert kb._unlocked_shields() == set()


def test_unlocked_shields_helper_empty_outside_app_context():
    # No app context -> get_setting raises -> safe empty default (all hidden).
    assert kb._unlocked_shields() == set()


from llama_index.core.vector_stores import FilterCondition, FilterOperator, MetadataFilter


def test_shield_filters_is_empty_only_when_nothing_unlocked():
    f = kb._shield_filters(set())
    assert len(f.filters) == 1
    flt = f.filters[0]
    assert isinstance(flt, MetadataFilter)
    assert flt.key == "shield"
    assert flt.operator == FilterOperator.IS_EMPTY


def test_shield_filters_adds_sorted_in_clause_when_unlocked():
    f = kb._shield_filters({"bob.notes", "alice.travel"})
    assert f.condition == FilterCondition.OR
    flts = [flt for flt in f.filters if isinstance(flt, MetadataFilter)]
    assert len(flts) == len(f.filters)
    ops = {flt.operator for flt in flts}
    assert FilterOperator.IS_EMPTY in ops and FilterOperator.IN in ops
    in_flt = next(flt for flt in flts if flt.operator == FilterOperator.IN)
    assert in_flt.key == "shield"
    assert in_flt.value == ["alice.travel", "bob.notes"]   # sorted


def test_exact_match_hidden_when_locked_and_shown_when_unlocked(monkeypatch):
    monkeypatch.setattr(kb, "_alias_table", {"who is alice": "u1"})
    monkeypatch.setattr(kb, "_entries_by_id",
                        {"u1": {"id": "u1", "shield": "alice.travel"}})
    assert kb._exact_match("Who is alice?", unlocked_shields=set()) is None
    m = kb._exact_match("Who is alice?", unlocked_shields={"alice.travel"})
    assert m is not None and m.qa_id == "u1"


def test_exact_match_unshielded_entry_unaffected(monkeypatch):
    monkeypatch.setattr(kb, "_alias_table", {"hello": "u2"})
    monkeypatch.setattr(kb, "_entries_by_id", {"u2": {"id": "u2"}})
    m = kb._exact_match("Hello?", unlocked_shields=set())
    assert m is not None and m.qa_id == "u2"


def test_retrieve_seed_memories_skips_locked(monkeypatch):
    app = db.make_app()
    with app.app_context():
        entries = {
            "u1": {"id": "u1", "path": "p.a", "kind": "static", "answer": "A",
                   "shield": "alice.travel", "_source": "upstream"},
            "u2": {"id": "u2", "path": "p.b", "kind": "static", "answer": "B",
                   "_source": "upstream"},
        }
        monkeypatch.setattr(kb, "_entries_by_id", entries)
        ranked = [Match(qa_id="u1", method="semantic", score=0.9),
                  Match(qa_id="u2", method="semantic", score=0.8)]
        out = kb.retrieve_seed_memories("x", _ranker=lambda q: ranked,
                                        unlocked_shields=set())
        assert [m.uuid for m in out] == ["u2"]          # u1 locked out
        out2 = kb.retrieve_seed_memories("x", _ranker=lambda q: ranked,
                                         unlocked_shields={"alice.travel"})
        assert [m.uuid for m in out2] == ["u1", "u2"]   # unlocked -> both


def test_available_qa_shields_sorted_distinct(monkeypatch):
    monkeypatch.setattr(kb, "_load_kb", lambda: None)   # registry pre-seeded below
    monkeypatch.setattr(kb, "_entries_by_id", {
        "a": {"id": "a", "shield": "bob.notes"},
        "b": {"id": "b", "shield": "alice.travel"},
        "c": {"id": "c", "shield": "alice.travel"},     # duplicate
        "d": {"id": "d"},                               # unshielded -> ignored
        "e": {"id": "e", "shield": ""},                 # empty -> ignored
    })
    assert kb.available_qa_shields() == ["alice.travel", "bob.notes"]


def test_available_qa_shields_empty_when_none(monkeypatch):
    monkeypatch.setattr(kb, "_load_kb", lambda: None)
    monkeypatch.setattr(kb, "_entries_by_id", {"a": {"id": "a"}})
    assert kb.available_qa_shields() == []
