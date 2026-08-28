"""Opt-in benchmark: does a derived roster surface on phrasings nobody authored?

Paraphrase reach is the property that distinguishes derived rosters from the
cheaper alias→prefix→enumerate design, which matches authored wording only. It
cannot be a unit test — the answer depends on `embeddinggemma`, its version and
the surrounding corpus — so it lives here and is run deliberately.

    DATABASE_URL=postgresql+psycopg://localhost/rainbox_claude \\
        python -m benchmarks.roster_paraphrase

Needs Ollama running with the embedding model. Builds a synthetic registry in a
throwaway pgvector table and drops it afterwards; touches nothing else.
"""
from __future__ import annotations

import json
import os
from uuid import uuid4

import psycopg
import sqlalchemy as sa
from llama_index.vector_stores.postgres import PGVectorStore

import db
import memory.seed_memory as kb

PREFIX = "human.subject.friend"
MEMBERS = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]
AUTHORED = ["who are my friends", "my friends", "list my friends"]

# Phrasings deliberately absent from AUTHORED. A roster that ranks first on
# these is reachable by meaning, not only by the words someone typed.
PARAPHRASES = [
    "who do I hang out with",
    "name the people close to me",
    "which people am I friendly with",
    "tell me about my social circle",
    "who are my mates",
    "people I spend time with",
]

# Distractors that outranked the real members in the measured failure: generic
# identity entries that any "who ..." question pulls.
DISTRACTORS = [
    ("identity.role", "Who are you?"),
    ("identity.name", "What is your name?"),
    # Carries the plural token that the real lexical signal latched onto.
    ("human.subject.cinemas", "which cinemas do I go to with friends"),
    ("human.subject.job", "What does the subject do for a living?"),
    ("human.subject.food", "What food does the subject like?"),
]


def _entries() -> list[dict]:
    out = [{"id": f"m-{n}", "path": f"{PREFIX}.{n}", "kind": "static",
            "questions": [f"who is {n}", f"tell me about {n}"],
            "answer": f"About {n}.", "label": n.capitalize()}
           for n in MEMBERS]
    out += [{"id": f"d-{i}", "path": path, "kind": "static",
             "questions": [q], "answer": f"Answer for {path}."}
            for i, (path, q) in enumerate(DISTRACTORS)]
    return out


def main() -> int:
    url = sa.engine.url.make_url(os.environ.get("DATABASE_URL", db.DEFAULT_DATABASE_URL))
    if url.database != "rainbox_claude":
        print(f"refusing to run against {url.database!r}; use rainbox_claude")
        return 2

    app = db.make_app()
    db.init_db(app)
    with app.app_context():
        table = f"roster_bench_{uuid4().hex[:8]}"
        vs = PGVectorStore.from_params(
            database=url.database, host=url.host or "127.0.0.1",
            port=str(url.port or 5432), user=url.username or "",
            password=url.password or "", table_name=table, embed_dim=768,
        )
        kb.QA_FULL_TABLE, kb._vs = f"data_{table}", vs
        entries = _entries()
        rosters = kb._synthesize_rosters(entries, [{
            "prefix": PREFIX, "title": "friends", "complete": False,
            "shield": None, "questions": AUTHORED}])
        kb._entries_by_id = {e["id"]: e for e in entries + rosters}
        kb._alias_table = kb._build_alias_table(entries + rosters)
        rid = rosters[0]["id"]

        try:
            from llama_index.core import StorageContext, VectorStoreIndex
            VectorStoreIndex.from_documents(
                kb._build_documents(entries + rosters),
                storage_context=StorageContext.from_defaults(vector_store=vs),
                embed_model=kb._embed_model())

            print(f"model        {kb.EMBED_MODEL_NAME}   epoch {kb.KB_EPOCH}")
            print(f"corpus       {len(entries)} authored entries "
                  f"({len(MEMBERS)} members, {len(DISTRACTORS)} distractors) + 1 roster")
            print(f"authored     {AUTHORED}")
            print()
            rows, wins = [], 0
            for q in AUTHORED + PARAPHRASES:
                ranked = kb._semantic_ranked(q, vs, unlocked_shields=set())
                order = [m.qa_id for m in ranked]
                rank = order.index(rid) + 1 if rid in order else None
                score = next((m.score for m in ranked if m.qa_id == rid), None)
                kind = "authored " if q in AUTHORED else "paraphrase"
                if rank == 1:
                    wins += 1
                rows.append({"query": q, "kind": kind.strip(), "rank": rank,
                             "score": None if score is None else round(score, 4)})
                print(f"  [{kind}] rank {str(rank):>4}  score "
                      f"{'—' if score is None else f'{score:.4f}'}   {q!r}")
            para = [r for r in rows if r["kind"] == "paraphrase"]
            first = sum(1 for r in para if r["rank"] == 1)
            print()
            print(f"paraphrases ranking the roster first: {first}/{len(para)}")
            print(f"all queries ranking it first:         {wins}/{len(rows)}")
            print()
            print(json.dumps({"model": kb.EMBED_MODEL_NAME, "results": rows}, indent=2))
        finally:
            db.session.rollback()
            with psycopg.connect(db.psycopg_dsn(), autocommit=True) as c, c.cursor() as cur:
                cur.execute(f'DROP TABLE IF EXISTS "data_{table}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
