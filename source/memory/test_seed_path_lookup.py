"""answers_by_path: a query that names a seed fact's path is a lookup — that
entry, then (for a dotted path) the entries filed under it, in path order.
Synthetic registry only."""
import memory.seed_memory as qkb
from agents.query_handlers import QueryContext

QCTX = QueryContext(room_uuid=None, query="", payload={}, agent_uuid=None)

ENTRIES = {
    "id-top": {"path": "garden", "answer": "A walled garden.", "_source": "user-overlay"},
    "id-shed": {"path": "garden.shed", "answer": "A wooden shed.", "_source": "user-overlay"},
    "id-roof": {"path": "garden.shed.roof", "answer": "Tar paper.", "_source": "user-overlay"},
    "id-door": {"path": "garden.shed.door", "answer": "Green paint.", "_source": "upstream"},
    "id-near": {"path": "garden.shedding", "answer": "Not under the shed.", "_source": "user-overlay"},
    "id-hidden": {"path": "garden.shed.key", "answer": "Under the mat.",
                  "_source": "user-overlay", "shield": "private.keys"},
}


def _lookup(monkeypatch, query, unlocked=frozenset()):
    monkeypatch.setattr(qkb, "_entries_by_id", ENTRIES)
    return qkb.answers_by_path(query, qctx=QCTX, unlocked_shields=set(unlocked))


def test_a_path_returns_its_entry_then_the_entries_under_it(monkeypatch):
    found = _lookup(monkeypatch, "garden.shed")
    assert [s.path for s in found] == ["garden.shed", "garden.shed.door", "garden.shed.roof"]
    assert found[0].answer == "A wooden shed." and found[0].uuid == "id-shed"
    assert found[1].source == "upstream"


def test_a_sibling_sharing_a_prefix_is_not_under_it(monkeypatch):
    assert "garden.shedding" not in [s.path for s in _lookup(monkeypatch, "garden.shed")]


def test_a_locked_entry_stays_hidden_until_its_shield_is_unlocked(monkeypatch):
    assert "garden.shed.key" not in [s.path for s in _lookup(monkeypatch, "garden.shed")]
    unlocked = _lookup(monkeypatch, "garden.shed", unlocked={"private.keys"})
    assert "garden.shed.key" in [s.path for s in unlocked]


def test_a_plain_word_matches_only_exactly(monkeypatch):
    """A one-segment query that names a path returns that entry alone: a plain
    search word that happens to be a top-level segment must not turn into a
    whole subtree."""
    assert [s.path for s in _lookup(monkeypatch, "garden")] == ["garden"]


def test_a_query_that_names_no_path_returns_nothing(monkeypatch):
    assert _lookup(monkeypatch, "wooden shed") == []
    assert _lookup(monkeypatch, "garden.pond") == []
    assert _lookup(monkeypatch, "  ") == []
