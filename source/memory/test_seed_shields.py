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
