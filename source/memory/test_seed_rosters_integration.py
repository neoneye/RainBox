"""Rosters in composition: persistence, shields, retrieval and the sync
lifecycle. The pure helpers are covered in test_seed_rosters.py; this file
proves the parts those helpers cannot — that a roster reaches Postgres, that a
shielded one occupies no vector budget, and that removing a declaration removes
its rows.

Fixtures are synthetic. No embedder and no model: the vector store is a
throwaway table with a fake embedder, in the style of test_seed_sync.py.
"""
import json
import os
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
import sqlalchemy as sa
from llama_index.vector_stores.postgres import PGVectorStore

import db
import memory.seed_memory as kb

PREFIX = "human.subject.friend"
QUESTION = "who are my friends"


def _decl(**over):
    d = {"prefix": PREFIX, "title": "friends", "complete": False,
         "shield": None, "questions": [QUESTION]}
    d.update(over)
    return d


def _member(name, **over):
    e = {"id": f"m-{name}", "path": f"{PREFIX}.{name}", "kind": "static",
         "questions": [f"who is {name}"], "answer": f"About {name}.",
         "label": name.capitalize()}
    e.update(over)
    return e


def _fake_vector(text: str) -> list[float]:
    v = [0.0] * 768
    for i, ch in enumerate(text[:768]):
        v[i] = (ord(ch) % 17) / 17.0
    return v


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Base JSONL + customize dir + throwaway pgvector table + fake embedder."""
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()

    table = f"seed_roster_test_{uuid4().hex[:8]}"
    url = sa.engine.url.make_url(os.environ["DATABASE_URL"])
    vs = PGVectorStore.from_params(
        database=url.database, host=url.host or "127.0.0.1",
        port=str(url.port or 5432), user=url.username or "",
        password=url.password or "", table_name=table, embed_dim=768,
    )
    monkeypatch.setattr(kb, "QA_FULL_TABLE", f"data_{table}")
    monkeypatch.setattr(kb, "_vs", vs)

    base = tmp_path / "question_answer.jsonl"
    base.write_text("")
    customize = tmp_path / "customize"
    customize.mkdir()
    overlay = customize / "question_answer.jsonl"
    overlay.write_text("")
    monkeypatch.setattr(kb, "QA_JSONL_PATH", base)
    monkeypatch.setattr(kb, "_overlay_path", lambda: overlay)
    monkeypatch.setattr(kb, "_populated", False)
    monkeypatch.setattr(kb, "_entries_by_id", {})
    monkeypatch.setattr(kb, "_alias_table", {})
    monkeypatch.setattr(kb, "_sync_snapshot", None)
    monkeypatch.setattr(kb, "_fulltext_index_cache", None)
    monkeypatch.setattr(kb, "_embed_texts",
                        lambda texts: [_fake_vector(t) for t in texts])

    def write(entries=(), relations=None):
        overlay.write_text("".join(json.dumps(e) + "\n" for e in entries))
        rel = customize / "relations.json"
        if relations is None:
            rel.unlink(missing_ok=True)
        else:
            rel.write_text(json.dumps({"relations": relations}))
        kb._entries_by_id, kb._alias_table = {}, {}
        kb._fulltext_index_cache = None

    prior_stamp = db.get_setting("qa.facts_invalidated_at")
    try:
        yield SimpleNamespace(write=write, vs=vs, table=f"data_{table}",
                              customize=customize)
    finally:
        db.db.session.rollback()
        db.set_setting("qa.facts_invalidated_at", prior_stamp)
        with psycopg.connect(db.psycopg_dsn(), autocommit=True) as c, c.cursor() as cur:
            cur.execute(f'DROP TABLE IF EXISTS "data_{table}"')
        ctx.pop()


def _node_rows(table):
    with psycopg.connect(db.psycopg_dsn(), autocommit=True) as c, c.cursor() as cur:
        cur.execute(f'SELECT metadata_ FROM "{table}"')
        return [r[0] for r in cur.fetchall()]


def _roster_nodes(table):
    return [m for m in _node_rows(table) if m.get("qa_id") == kb._roster_id(PREFIX)]


# --- test 8b: authored wording surfaces the roster, no embedder involved -----


def test_authored_wording_surfaces_the_roster_lexically(env):
    env.write([_member("alpha"), _member("beta")], relations=[_decl()])
    kb._load_kb()
    # top_k_vector=0 disables the embedding signal: the assertion is purely
    # lexical, so it holds without a model.
    ranked = kb._hybrid_seed_ranked(QUESTION, env.vs, top_k_vector=0,
                                    unlocked_shields=set())
    assert ranked and ranked[0].qa_id == kb._roster_id(PREFIX)


# --- test 15: invalid configuration mutates nothing, on both write paths -----


def test_invalid_relations_leave_the_table_untouched_on_sync(env):
    env.write([_member("alpha"), _member("beta")], relations=[_decl()])
    kb.sync_kb()
    before = sorted(json.dumps(m, sort_keys=True) for m in _node_rows(env.table))
    assert before

    (env.customize / "relations.json").write_text('{"relations": [{"prefix": ""}]}')
    kb._sync_snapshot = None
    with pytest.raises(ValueError):
        kb.sync_kb()
    assert sorted(json.dumps(m, sort_keys=True)
                  for m in _node_rows(env.table)) == before


def test_invalid_relations_leave_the_table_untouched_on_rebuild(env):
    env.write([_member("alpha")], relations=[_decl()])
    kb.sync_kb()
    before = len(_node_rows(env.table))
    assert before

    (env.customize / "relations.json").write_text('{"relations": [{"prefix": ""}]}')
    with pytest.raises(ValueError):
        kb.rebuild_kb()
    assert len(_node_rows(env.table)) == before, "rebuild truncated before validating"


# --- test 17: eligible for the always-on chat block --------------------------


def test_roster_is_eligible_for_the_always_on_chat_block(env):
    env.write([_member("alpha"), _member("beta")], relations=[_decl()])
    kb._load_kb()
    rid = kb._roster_id(PREFIX)
    # Inject the ranker: this asserts the static-only filter, not the embedder.
    seeds = kb.retrieve_seed_memories(
        QUESTION,
        _ranker=lambda q: [kb.Match(qa_id=rid, method="test", score=1.0)],
        unlocked_shields=set(),
    )
    assert [s.uuid for s in seeds] == [rid]
    assert seeds[0].source == "user-overlay"
    assert "Alpha" in seeds[0].answer


# --- test 18: a locked roster occupies no vector budget ----------------------


def test_locked_roster_is_excluded_in_sql_and_visible_when_unlocked(env):
    env.write([_member("alpha", shield="private"), _member("beta", shield="private")],
              relations=[_decl(shield="private")])
    kb.sync_kb()

    nodes = _roster_nodes(env.table)
    assert nodes, "roster was not embedded"
    # The shield rides in node metadata, so _shield_filters excludes the roster
    # in SQL rather than after it has consumed a top-K slot.
    assert all(m.get("shield") == "private" for m in nodes)

    locked = kb._hybrid_seed_ranked(QUESTION, env.vs, top_k_vector=0,
                                    unlocked_shields=set())
    assert kb._roster_id(PREFIX) not in {m.qa_id for m in locked}

    unlocked = kb._hybrid_seed_ranked(QUESTION, env.vs, top_k_vector=0,
                                      unlocked_shields={"private"})
    assert kb._roster_id(PREFIX) in {m.qa_id for m in unlocked}


# --- test 19: mismatch suppresses one roster, registry still loads -----------


def test_shield_mismatch_suppresses_only_the_roster(env):
    env.write([_member("alpha"), _member("beta", shield="private")],
              relations=[_decl(shield=None)])
    entries = kb._load_jsonl()
    assert all(e.get("_derived") != "roster" for e in entries)
    # The complete authored registry still loads — suppression, not raising.
    assert {e["id"] for e in entries} == {"m-alpha", "m-beta"}


# --- test 22: every printed id dereferences ----------------------------------


def test_every_printed_id_resolves_through_the_registry(env):
    env.write([_member("alpha"), _member("beta"), _member("gamma")],
              relations=[_decl()])
    kb._load_kb()
    roster = kb.get_entry(kb._roster_id(PREFIX))
    printed = [line.split("[")[1].rstrip("]")
               for line in roster["answer"].split("\n")[1:]]
    assert printed == ["m-alpha", "m-beta", "m-gamma"]
    for qa_id in printed:
        # memory_query's uuid mode reads the registry; an unresolvable id would
        # make the roster a display list rather than an index card.
        assert kb.get_entry(qa_id) is not None


# --- tests 23/24: sync reacts to declaration edits and removal ---------------


def test_editing_a_declaration_re_embeds_the_roster(env):
    env.write([_member("alpha"), _member("beta")], relations=[_decl()])
    kb.sync_kb()
    before = {m["question"] for m in _roster_nodes(env.table)}
    assert before == {QUESTION}

    env.write([_member("alpha"), _member("beta")],
              relations=[_decl(questions=[QUESTION, "list my friends"])])
    kb._sync_snapshot = None
    counts = kb.sync_kb()
    assert counts["embedded"] or counts["updated"]
    assert {m["question"] for m in _roster_nodes(env.table)} == {
        QUESTION, "list my friends"}


def test_flipping_complete_re_renders_the_stored_answer(env):
    env.write([_member("alpha")], relations=[_decl()])
    kb.sync_kb()
    assert _roster_nodes(env.table)[0]["answer"].startswith("recorded friends (1):")

    env.write([_member("alpha")], relations=[_decl(complete=True)])
    kb._sync_snapshot = None
    kb.sync_kb()
    assert _roster_nodes(env.table)[0]["answer"].startswith("friends (1):")


def test_removing_relations_json_deletes_the_roster_nodes(env):
    env.write([_member("alpha"), _member("beta")], relations=[_decl()])
    kb.sync_kb()
    assert _roster_nodes(env.table)

    env.write([_member("alpha"), _member("beta")], relations=None)
    kb._sync_snapshot = None
    kb.sync_kb()
    assert _roster_nodes(env.table) == []
    # The authored members are untouched.
    assert {m["qa_id"] for m in _node_rows(env.table)} == {"m-alpha", "m-beta"}


def test_sync_invalidates_the_registry_so_a_roster_edit_is_visible(env):
    env.write([_member("alpha")], relations=[_decl()])
    kb.sync_kb()
    kb._load_kb()
    assert "(1)" in kb.get_entry(kb._roster_id(PREFIX))["answer"]

    env.write([_member("alpha"), _member("beta")], relations=[_decl()])
    kb._sync_snapshot = None
    kb.sync_kb()
    kb._load_kb()
    assert "(2)" in kb.get_entry(kb._roster_id(PREFIX))["answer"]
