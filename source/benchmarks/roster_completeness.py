"""Opt-in benchmark: does an incomplete roster stop a model inferring absence?

A roster lists the entries that exist under a prefix. When the operator has not
asserted `complete`, the list is a sample, not a census — so asked about
somebody who is not on it, the honest answer is "not recorded", never "no".

Whether the rendering achieves that is a claim about model behaviour, not about
the code, so it cannot be a unit test. It is measured here, deliberately, and
should be re-run after any change to the caveat wording or the model.

    DATABASE_URL=postgresql+psycopg://localhost/rainbox_claude \\
        python -m benchmarks.roster_completeness

    ROSTER_BENCH_MODELS=llama3.2:3b,qwen3.5:9b  …   # override the model list

Needs Ollama running. Makes no database or vector-store changes at all; it only
needs an app context to construct the LLM.
"""
from __future__ import annotations

import os

import db
import memory.seed_memory as kb
from llm import prepare_llm

MODELS = os.environ.get("ROSTER_BENCH_MODELS", "llama3.2:3b,qwen3.5:9b").split(",")

PREFIX = "human.subject.friend"
# Deceased namesakes only, per the rule for shipped examples.
MEMBERS = ["Albert Einstein", "Niels Bohr", "Marie Curie",
           "Paul Dirac", "Emmy Noether", "Lise Meitner"]
ABSENT = "Isaac Newton"

# The question that separates the two renderings. A census supports "no"; a
# sample supports only "not recorded".
QUESTION = f"Is {ABSENT} one of my friends?"

SYSTEM = ("You answer strictly from the recalled memory provided. "
          "Be brief — one or two sentences.")


def _roster(complete: bool) -> str:
    decl = {"prefix": PREFIX, "title": "friends", "complete": complete,
            "shield": None, "questions": ["who are my friends"]}
    members = [{"id": f"m-{i}", "path": f"{PREFIX}.m{i}", "label": name}
               for i, name in enumerate(MEMBERS)]
    return kb._render_roster(decl, members)


# Phrases that say "this record does not settle it" rather than "the answer is
# no". The caveat's whole job is to make one of these appear.
_LIMIT_PHRASES = (
    "not evidence", "not necessarily", "may be incomplete", "not exhaustive",
    "does not mean", "doesn't mean", "only the recorded", "only reflects",
    "may still", "might still", "cannot conclude", "can't conclude",
    "not conclusive", "absence",
)


def _cites_limit(answer: str) -> bool:
    """Did the answer tell the reader the record is not the world?

    Scoping to the record ("not on the list") is not enough — a model does that
    naturally, with or without a caveat, and it still leaves a reader free to
    read it as "no". What the caveat is for is the further step of naming the
    limit."""
    low = answer.lower()
    return any(w in low for w in _LIMIT_PHRASES)


def main() -> int:
    import sqlalchemy as sa
    url = sa.engine.url.make_url(os.environ.get("DATABASE_URL", db.DEFAULT_DATABASE_URL))
    if url.database != "rainbox_claude":
        print(f"refusing to run against {url.database!r}; use rainbox_claude")
        return 2

    app = db.make_app()
    db.init_db(app)
    with app.app_context():
        print(f"question   {QUESTION!r}")
        print(f"absent     {ABSENT} is in neither rendering")
        print(f"members    {len(MEMBERS)}\n")
        for name in MODELS:
            name = name.strip()
            if not name:
                continue
            try:
                the_llm = prepare_llm("ollama", name, {"request_timeout": 180.0})
            except Exception as exc:                      # noqa: BLE001
                print(f"{name}: could not prepare ({exc})\n")
                continue
            print(f"=== {name} ===")
            for complete in (False, True):
                prompt = (f"{SYSTEM}\n\n<recalled_memory>\n{_roster(complete)}\n"
                          f"</recalled_memory>\n\n{QUESTION}")
                try:
                    answer = str(the_llm.complete(prompt)).strip().replace("\n", " ")
                except Exception as exc:                  # noqa: BLE001
                    print(f"  complete={str(complete):<5} ERROR {exc}")
                    continue
                label = "census" if complete else "sample"
                cited = "cites-limit" if _cites_limit(answer) else "flat-answer"
                print(f"  complete={str(complete):<5} [{label}] "
                      f"{cited:<12} {answer[:170]}")
            print()
        print("Reading it: the caveat is working for a model when complete=false")
        print("says cites-limit and complete=true does not. Same list, same")
        print("question — only the caveat differs. A model that reads flat-answer")
        print("in both rows is ignoring the caveat, and for that model the")
        print("rendering buys provenance in the stored data but no behaviour.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
