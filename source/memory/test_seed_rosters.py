"""Derived rosters: a declared relation becomes an ordinary `static` entry
whose answer lists the entries filed under its path prefix.

Everything here is pure — parsing, membership, rendering, digests, synthesis.
No database, no embedder, no model. The retrieval-side and route-level tests
live with their own modules.

All fixtures are synthetic. The operator's real relations live only under
`customize.dir`.
"""
import json
from uuid import UUID

import pytest

import memory.seed_memory as kb


def _decl(**over):
    d = {
        "prefix": "human.subject.friend",
        "title": "friends",
        "complete": False,
        "shield": None,
        "questions": ["who are my friends"],
    }
    d.update(over)
    return d


def _entry(path, **over):
    e = {"id": path, "path": path, "kind": "static",
         "questions": [path], "answer": "x"}
    e.update(over)
    return e


# Pinned in test_digest_is_pinned_including_non_ascii; see that test.
_PINNED_DIGEST = "5cfb666ed8e4b71e84cfba899aa80737ea8b1aad55781f6c0299e390411530d0"


def _members(n, prefix="human.subject.friend"):
    return [_entry(f"{prefix}.p{i}", label=f"Person {i}") for i in range(n)]


# --- relations.json parsing --------------------------------------------------


def test_valid_declaration_parses():
    assert kb._parse_relations({"relations": [_decl()]}) == [_decl()]


def test_missing_file_yields_no_relations(tmp_path, monkeypatch):
    monkeypatch.setattr(kb, "_relations_path",
                        lambda overlay=None: tmp_path / "absent.json")
    assert kb._load_relations() == []


@pytest.mark.parametrize("doc, match", [
    ({}, "relations"),
    ({"relations": {}}, "must be a list"),
    ({"relations": [[]]}, "must be an object"),
    ({"relations": [_decl(prefix=None)]}, "prefix"),
    ({"relations": [_decl(prefix="a.b.")]}, "empty segment"),
    ({"relations": [_decl(prefix=".a")]}, "empty segment"),
    ({"relations": [_decl(prefix="a b\nc")]}, "newline"),
    ({"relations": [_decl(title="")]}, "title"),
    ({"relations": [_decl(title="   ")]}, "title"),
    ({"relations": [_decl(title=12)]}, "title"),
    ({"relations": [_decl(questions=[])]}, "questions"),
    ({"relations": [_decl(questions=["ok", 5])]}, "question"),
    ({"relations": [_decl(questions=["   "])]}, "question"),
    ({"relations": [_decl(questions=["???"])]}, "question"),
    ({"relations": [_decl(questions=["What is X?", "what is x"])]}, "collapse"),
    ({"relations": [_decl(complete="yes")]}, "complete"),
    ({"relations": [_decl(shield=12)]}, "shield"),
    ({"relations": [_decl(shield="")]}, "shield"),
    ({"relations": [_decl(), _decl()]}, "duplicate prefix"),
])
def test_invalid_declarations_raise(doc, match):
    with pytest.raises(ValueError, match=match):
        kb._parse_relations(doc)


def test_shield_key_must_be_present():
    d = _decl()
    del d["shield"]
    with pytest.raises(ValueError, match="shield"):
        kb._parse_relations({"relations": [d]})


# --- membership --------------------------------------------------------------


def test_membership_is_one_non_empty_segment_below_the_prefix():
    entries = [
        _entry("human.subject.friend.alpha"),                 # member
        _entry("human.subject.friend.beta"),                  # member
        _entry("human.subject.friend.alpha.travel"),          # deeper: not
        _entry("human.subject.friend"),                       # the prefix: not
        _entry("human.subject.friend."),                      # empty segment: not
        _entry("human.subject.friendship.x"),                 # prefix-of-string: not
        _entry("human.other.friend.gamma"),                   # other subject: not
    ]
    got = [e["path"] for e in kb._roster_members(entries, "human.subject.friend")]
    assert got == ["human.subject.friend.alpha", "human.subject.friend.beta"]


def test_membership_keeps_source_order():
    entries = [_entry("human.subject.friend.z"), _entry("human.subject.friend.a")]
    got = [e["path"] for e in kb._roster_members(entries, "human.subject.friend")]
    assert got == ["human.subject.friend.z", "human.subject.friend.a"]


# --- rendering ---------------------------------------------------------------


def test_render_lists_every_member_when_it_fits():
    text = kb._render_roster(_decl(), _members(3))
    assert text.startswith("recorded friends (3):")
    for i in range(3):
        assert f"- Person {i}  [human.subject.friend.p{i}]" in text
    assert "omitted" not in text


def test_render_complete_drops_the_recorded_qualifier():
    assert kb._render_roster(_decl(complete=True), _members(2)).startswith("friends (2):")


def test_render_zero_members():
    text = kb._render_roster(_decl(), [])
    assert text == "recorded friends (0):"


def test_render_label_falls_back_to_the_final_path_segment():
    text = kb._render_roster(_decl(), [_entry("human.subject.friend.alpha")])
    assert "- alpha  [human.subject.friend.alpha]" in text


def test_render_truncates_at_a_member_boundary():
    members = [_entry(f"human.subject.friend.p{i}", label=f"Person {i:03d}")
               for i in range(200)]
    text = kb._render_roster(_decl(), members)
    assert len(text) <= kb.ROSTER_ANSWER_MAX_CHARS
    assert text.startswith("recorded friends (200):")
    lines = text.split("\n")
    marker = lines[-1]
    shown = len(lines) - 2                      # header + marker
    assert marker == f"- … {200 - shown} additional recorded members omitted"
    # Every shown line is whole: no sliced label, no sliced id.
    for line in lines[1:-1]:
        assert line.startswith("- Person ") and line.endswith("]")


def test_render_permits_zero_displayed_members():
    long_title = "t" * (kb.ROSTER_ANSWER_MAX_CHARS - 80)
    text = kb._render_roster(_decl(title=long_title), _members(5))
    assert len(text) <= kb.ROSTER_ANSWER_MAX_CHARS
    assert text.split("\n")[-1] == "- … 5 additional recorded members omitted"


def test_render_rejects_a_title_too_long_to_render_anything():
    with pytest.raises(ValueError, match="too long"):
        kb._render_roster(_decl(title="t" * kb.ROSTER_ANSWER_MAX_CHARS), _members(1))


@pytest.mark.parametrize("bad", ["Per\nson", "Per\rson"])
def test_render_rejects_newlines_in_a_label(bad):
    with pytest.raises(ValueError, match="newline"):
        kb._render_roster(_decl(), [_entry("human.subject.friend.a", label=bad)])


@pytest.mark.parametrize("bad", ["", "   ", 12, None])
def test_render_rejects_a_present_but_unusable_label(bad):
    with pytest.raises(ValueError, match="label"):
        kb._render_roster(_decl(), [_entry("human.subject.friend.a", label=bad)])


def test_render_rejects_a_non_string_member_id():
    m = _entry("human.subject.friend.a")
    m["id"] = 12
    with pytest.raises(ValueError, match="id"):
        kb._render_roster(_decl(), [m])


# --- identity and digest -----------------------------------------------------


def test_namespace_is_pinned():
    assert kb._ROSTER_NS == UUID("94cacd83-3427-5460-80c5-239a56244707")


def test_roster_id_is_pinned_and_stable():
    # Rows outlive the code that wrote them: a namespace or scheme change must
    # not pass silently, so this is pinned against a literal.
    assert kb._roster_id("human.subject.friend") == "215f614f-1c4d-5d2b-b4b0-792409c0265a"


def test_canonical_encoding_is_pinned():
    # sort_keys, compact separators, and unescaped non-ASCII encoded as UTF-8.
    assert kb._canonical({"b": 1, "a": "æø"}) == '{"a":"æø","b":1}'.encode("utf-8")


def test_digest_is_pinned_including_non_ascii():
    # Compared against a literal, not against itself: a change to the
    # canonical encoding (escaping, separators, key order) must fail here
    # rather than quietly orphaning every embedded roster row.
    decl = _decl(title="vænner")
    members = [_entry("human.subject.friend.p0", _row_sha256="h0"),
               _entry("human.subject.friend.p1", _row_sha256="h1")]
    assert kb._roster_digest(decl, members) == _PINNED_DIGEST


@pytest.mark.parametrize("mutate", [
    lambda d, m: (d | {"title": "other"}, m),
    lambda d, m: (d | {"complete": True}, m),
    lambda d, m: (d | {"shield": "private"}, m),
    lambda d, m: (d | {"questions": ["different"]}, m),
    lambda d, m: (d, list(reversed(m))),
    lambda d, m: (d, m[:1]),
    lambda d, m: (d, m + [_entry("human.subject.friend.extra")]),
    lambda d, m: (d, [m[0] | {"_row_sha256": "changed"}] + m[1:]),
])
def test_digest_changes_on_every_observable_input(mutate):
    decl, members = _decl(), [_entry(f"human.subject.friend.p{i}",
                                     _row_sha256=f"h{i}") for i in range(2)]
    base = kb._roster_digest(decl, members)
    assert kb._roster_digest(*mutate(decl, members)) != base


def test_digest_ignores_an_entry_outside_the_prefix():
    # Synthesize twice, once with an unrelated entry present in the registry.
    decl = _decl()
    entries = _members(2)
    [before] = kb._synthesize_rosters(entries, [decl])
    [after] = kb._synthesize_rosters(entries + [_entry("somewhere.else")], [decl])
    assert after["_row_sha256"] == before["_row_sha256"]
    assert after["answer"] == before["answer"]


# --- synthesis ---------------------------------------------------------------


def test_synthesis_produces_a_static_overlay_entry():
    [roster] = kb._synthesize_rosters(_members(2), [_decl()])
    assert roster["kind"] == "static"
    assert roster["path"] == "human.subject.friend"
    assert roster["questions"] == ["who are my friends"]
    assert roster["_source"] == "user-overlay"
    assert roster["_derived"] == "roster"
    assert roster["_row_sha256"]
    assert "shield" not in roster           # declared null ⇒ absent, not None


@pytest.mark.parametrize("n", [0, 1, 6, 7])
def test_rosters_synthesize_at_every_cardinality(n):
    [roster] = kb._synthesize_rosters(_members(n), [_decl()])
    assert roster["questions"] == ["who are my friends"]
    assert roster["answer"].startswith(f"recorded friends ({n}):")


def test_declared_shield_is_stamped():
    members = [_entry("human.subject.friend.a", shield="private")]
    [roster] = kb._synthesize_rosters(members, [_decl(shield="private")])
    assert roster["shield"] == "private"


def test_zero_member_roster_carries_the_declared_shield():
    # Nothing to infer from: getting this wrong publishes the declaration's own
    # title and questions.
    [roster] = kb._synthesize_rosters([], [_decl(shield="private")])
    assert roster["shield"] == "private"
    [open_roster] = kb._synthesize_rosters([], [_decl(shield=None)])
    assert "shield" not in open_roster


@pytest.mark.parametrize("declared, member_shield", [
    (None, "private"),
    ("private", None),
    ("private", "other"),
])
def test_a_member_shield_differing_from_the_declaration_suppresses(declared, member_shield):
    m = _entry("human.subject.friend.a")
    if member_shield:
        m["shield"] = member_shield
    assert kb._synthesize_rosters([m], [_decl(shield=declared)]) == []


def test_duplicate_member_paths_raise():
    members = [_entry("human.subject.friend.a"),
               _entry("human.subject.friend.a", id="other")]
    with pytest.raises(ValueError, match="share the path"):
        kb._synthesize_rosters(members, [_decl()])


def test_an_authored_entry_at_the_roster_path_raises():
    authored = _entry("human.subject.friend", id="authored-1")
    entries = _members(2) + [authored]
    with pytest.raises(ValueError) as ei:
        kb._synthesize_rosters(entries, [_decl()])
    msg = str(ei.value)
    assert "already occupied" in msg
    assert "authored-1" in msg, f"error should name the authored entry: {msg!r}"


def test_an_authored_entry_holding_the_generated_id_raises():
    # _load_kb keys the registry by id, so an authored entry sharing the
    # roster's generated id would be silently replaced by it — the authored
    # answer gone, with nothing reported.
    rid = kb._roster_id("human.subject.friend")
    authored = _entry("somewhere.else", id=rid, answer="AUTHORED")
    with pytest.raises(ValueError) as ei:
        kb._synthesize_rosters(_members(2) + [authored], [_decl()])
    msg = str(ei.value)
    assert rid in msg
    assert "human.subject.friend" in msg
    assert "somewhere.else" in msg


def test_a_roster_is_never_a_member_of_another_roster():
    # A roster at human.subject.friend is a legal member of a declaration for
    # human.subject, so synthesis must run over frozen authored entries.
    inner = _decl()
    outer = _decl(prefix="human.subject", title="relations",
                  questions=["my relations"])
    entries = _members(2)

    def by_id(decls):
        return {r["id"]: r for r in kb._synthesize_rosters(entries, decls)}

    forward, reverse = by_id([inner, outer]), by_id([outer, inner])
    assert forward.keys() == reverse.keys()
    for rid in forward:
        for field in ("answer", "path", "_row_sha256", "questions"):
            assert forward[rid][field] == reverse[rid][field]
    # The outer roster saw no roster among its members.
    outer_id = kb._roster_id("human.subject")
    assert "relations (0)" in forward[outer_id]["answer"]


def test_authored_underscore_fields_are_rejected():
    # _derived drives full-text exclusion; an authored entry must not be able
    # to claim it and suppress indexing of its own answer.
    with pytest.raises(ValueError, match="reserved"):
        kb._reject_reserved_keys({"id": "a", "_derived": "roster"}, "f.jsonl", 1)


# --- wiring: loader, snapshot, lexical index ---------------------------------


@pytest.fixture
def customize(tmp_path, monkeypatch):
    """A base JSONL plus an overlay directory, with relations.json optional.
    _relations_path derives from _overlay_path, so stubbing the overlay is
    enough to place both files."""
    base = tmp_path / "question_answer.jsonl"
    base.write_text("")
    overlay_dir = tmp_path / "customize"
    overlay_dir.mkdir()
    overlay = overlay_dir / "question_answer.jsonl"
    monkeypatch.setattr(kb, "QA_JSONL_PATH", base)
    monkeypatch.setattr(kb, "_overlay_path", lambda: overlay)

    def write(entries, relations=None):
        overlay.write_text("".join(json.dumps(e) + "\n" for e in entries))
        if relations is not None:
            (overlay_dir / "relations.json").write_text(json.dumps({"relations": relations}))
    return write, overlay_dir


def test_load_jsonl_appends_the_synthesized_roster(customize):
    write, _ = customize
    write([_entry("human.subject.friend.a", label="Alpha"),
           _entry("human.subject.friend.b", label="Beta")],
          relations=[_decl()])
    entries = kb._load_jsonl()
    roster = next(e for e in entries if e.get("_derived") == "roster")
    assert roster["id"] == kb._roster_id("human.subject.friend")
    assert roster["kind"] == "static"
    assert "Alpha" in roster["answer"] and "Beta" in roster["answer"]
    # The authored entries are still there, untouched.
    assert sum(1 for e in entries if e.get("_derived") != "roster") == 2


def test_load_jsonl_without_relations_file_synthesizes_nothing(customize):
    write, _ = customize
    write([_entry("human.subject.friend.a")])
    assert all(e.get("_derived") != "roster" for e in kb._load_jsonl())


def test_authored_entry_claiming_a_loader_key_is_rejected_with_file_and_line(customize):
    write, _ = customize
    write([_entry("human.subject.friend.a"),
           dict(_entry("human.subject.friend.b"), _derived="roster")])
    with pytest.raises(ValueError) as ei:
        kb._load_jsonl()
    msg = str(ei.value)
    assert "_derived" in msg and "reserved" in msg and ":2" in msg


def test_source_snapshot_tracks_relations_json(customize):
    write, overlay_dir = customize
    write([_entry("human.subject.friend.a")], relations=[_decl()])
    before = kb._source_snapshot()
    relations = overlay_dir / "relations.json"
    assert str(relations) in before
    # An edit to the declarations alone must move the snapshot, or the
    # automatic reconcile never notices.
    relations.write_text(json.dumps(
        {"relations": [_decl(questions=["who are my friends", "my friends"])]}))
    assert kb._source_snapshot() != before


def test_roster_answer_is_absent_from_the_lexical_index(monkeypatch):
    entries = {
        "m1": {"kind": "static", "path": "human.subject.friend.a",
               "questions": ["Who is Zephyrine?"], "answer": "A friend."},
        "roster": {"kind": "static", "path": "human.subject.friend",
                   "questions": ["who are my friends"],
                   "answer": "recorded friends (1):\n- Zephyrine  [m1]",
                   "_derived": "roster"},
    }
    monkeypatch.setattr(kb, "_entries_by_id", entries)
    monkeypatch.setattr(kb, "_fulltext_index_cache", None)
    docs, _idf = kb._fulltext_index(set())
    by_id = {qa_id: (q_tokens, a_tokens) for qa_id, _pq, q_tokens, a_tokens in docs}

    # The roster contributes its questions and nothing else.
    assert by_id["roster"][1] == set()
    assert "zephyrine" not in by_id["roster"][0]
    # A member-name query reaches the member, never the roster.
    ranked = kb._fulltext_ranked("Zephyrine", unlocked_shields=set())
    assert [m.qa_id for m in ranked] == ["m1"]
    # The roster is still reachable by its own authored wording.
    assert kb._fulltext_ranked(
        "who are my friends", unlocked_shields=set())[0].qa_id == "roster"


def test_roster_questions_become_vector_documents():
    [roster] = kb._synthesize_rosters(_members(2), [_decl()])
    docs = kb._build_documents([roster])
    assert [d.text for d in docs] == ["who are my friends"]
    # The answer rides along as excluded metadata, never as embedded text.
    assert docs[0].metadata["answer"] == roster["answer"]
    assert "answer" in docs[0].excluded_embed_metadata_keys
