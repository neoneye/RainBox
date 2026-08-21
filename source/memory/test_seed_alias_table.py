"""The exact-alias table maps one normalized question to every entry that
claims it, so a question text appearing on two entries no longer silently
keeps whichever was loaded last.

`_exact_match` answers only when exactly one *visible* entry claims the alias.
Two visible claimants mean the alias is ambiguous, and an ambiguous alias is
not an answer: exact matching declines and each caller falls through to its own
retrieval path (three different ones — see the proposal).
"""
import memory.seed_memory as kb


def _install(monkeypatch, entries):
    """Build the registry and alias table from entries the way `_load_kb` does,
    so these tests exercise the real construction rather than a hand-written
    table."""
    monkeypatch.setattr(kb, "_entries_by_id",
                        {e["id"]: e for e in entries if e.get("id")})
    monkeypatch.setattr(kb, "_alias_table", kb._build_alias_table(entries))


def test_unique_alias_resolves(monkeypatch):
    _install(monkeypatch, [{"id": "u1", "questions": ["Who is Alice?"]}])
    m = kb._exact_match("who is alice", unlocked_shields=set())
    assert m is not None and m.qa_id == "u1"
    assert m.score == 1.0


def test_two_entries_sharing_an_alias_are_both_kept_and_decline(monkeypatch):
    # The live overlay has one alias shared by six entries. Keeping only the
    # last one is the silent data loss this fixes.
    _install(monkeypatch, [
        {"id": "u1", "questions": ["a friend"]},
        {"id": "u2", "questions": ["a friend"]},
    ])
    assert kb._alias_table["a friend"] == ["u1", "u2"]
    assert kb._exact_match("a friend", unlocked_shields=set()) is None


def test_one_entry_with_two_normalizing_variants_still_resolves(monkeypatch):
    # The shipped base registry carries entries whose own question lists
    # collapse under _normalize_query (casing, a trailing '?'). Without
    # deduplication by qa_id the alias would map to [u1, u1], read as two
    # claimants, and a lookup that works today would stop working.
    _install(monkeypatch, [{"id": "u1", "questions": ["What is MCP?", "what is mcp"]}])
    assert kb._alias_table["what is mcp"] == ["u1"]
    m = kb._exact_match("What is MCP?", unlocked_shields=set())
    assert m is not None and m.qa_id == "u1"


def test_alias_order_is_preserved(monkeypatch):
    _install(monkeypatch, [
        {"id": "u2", "questions": ["shared"]},
        {"id": "u1", "questions": ["shared"]},
    ])
    assert kb._alias_table["shared"] == ["u2", "u1"]


def test_locked_claimant_is_discarded_before_the_ambiguity_decision(monkeypatch):
    # Shield filtering precedes the count: one visible claimant is an answer
    # even when a locked entry claims the same alias.
    _install(monkeypatch, [
        {"id": "u1", "questions": ["a friend"]},
        {"id": "u2", "questions": ["a friend"], "shield": "private"},
    ])
    m = kb._exact_match("a friend", unlocked_shields=set())
    assert m is not None and m.qa_id == "u1"
    # Unlocking the second makes the alias genuinely ambiguous again.
    assert kb._exact_match("a friend", unlocked_shields={"private"}) is None


def test_all_claimants_locked_declines(monkeypatch):
    _install(monkeypatch, [
        {"id": "u1", "questions": ["a friend"], "shield": "private"},
        {"id": "u2", "questions": ["a friend"], "shield": "private"},
    ])
    assert kb._exact_match("a friend", unlocked_shields=set()) is None


def test_entry_without_id_contributes_no_alias(monkeypatch):
    _install(monkeypatch, [
        {"questions": ["orphan"]},
        {"id": "u1", "questions": ["orphan"]},
    ])
    assert kb._alias_table["orphan"] == ["u1"]
