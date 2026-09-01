"""The relevance filter over recalled candidates: its schema, its prompt, and
the keep/drop policy applied to what the scorer returns.

The scorer only scores. Whether a candidate survives is decided here, in code
(`apply_filter_scores`), from those scores — so the policy is one readable
rule rather than something a prompt has to be trusted to apply consistently
across models.

Candidate-kind agnostic on purpose: `build_filter_prompt_rows` takes prepared
rows, so one filter call ranks seed Q&A entries and memory claims side by side
under the same policy. `agents/recall_reranker.py` is the other backend that
fills `ScoredCandidate` in, scoring with a cross-encoder instead of an LLM.
"""

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from memory.seed_memory import Match, get_entry, score_permille


class FilterScore(BaseModel):
    """One candidate's relevance scores from the filter LLM. The LLM only
    scores; keeping or dropping is decided in code (`apply_filter_scores`).

    The scales are `Literal[1..5]` rather than a bare `int`: that renders as a
    JSON-schema enum, which a grammar-constrained decoder enforces, so a model
    cannot answer 7 or 3.5. A plain `int` with ge/le would leave the bound to
    validation, which costs a whole model attempt when it trips.
    """

    id: str = Field(
        description="The candidate's id, copied verbatim from the list."
    )
    direct: Literal[1, 2, 3, 4, 5] = Field(
        description=(
            "How directly this candidate answers the user's message: "
            "1 = does not answer it at all, 5 = answers it outright."
        )
    )
    indirect: Literal[1, 2, 3, 4, 5] = Field(
        description=(
            "How much closely related context this candidate adds without "
            "answering the message itself (e.g. the family or household of a "
            "person the user asks about): 1 = no related context, "
            "5 = strongly related context."
        )
    )
    relevancy: Literal[1, 2, 3, 4, 5] = Field(
        description=(
            "Overall topical relevance to the user's message: "
            "1 = a different topic entirely, 5 = the same topic."
        )
    )


class FilterDecision(BaseModel):
    """Output of the filter LLM call: a self-calibration note, then a score
    row per listed candidate. `reasoning` is declared BEFORE `items` on
    purpose — schema property order follows field order, so the model writes
    its overall does-anything-match assessment first and the scores are
    conditioned on it. The note also travels with the results: the assistant
    reads it in the memory_query observation when assessing what to do next."""

    reasoning: str = Field(
        description=(
            "First, in 1-3 short sentences: does any candidate genuinely "
            "match the user's message, and why or why not. Written BEFORE "
            "scoring, to calibrate the scores that follow."
        )
    )
    items: list[FilterScore] = Field(
        description=(
            "One score row for every candidate in the list — omit none, "
            "invent none."
        )
    )


FILTER_SYSTEM_PROMPT: str = """\
You are a relevance scorer. Given the user's latest chat message and a list of
candidates — knowledge-base Q&A entries and/or remembered facts — score EVERY
candidate on three Likert scales from 1 (not at all) to 5 (fully):

- `direct`: how directly the candidate's question/answer addresses what the
  user is asking, telling, or doing (1 = not at all, 5 = answers it
  outright).
- `indirect`: how much closely related context the candidate adds without
  answering the message itself — e.g. for a question about a person, an entry
  about that person's family or household (1 = none, 5 = strongly
  related).
- `relevancy`: overall topical relevance to the message (1 = a different
  topic entirely, 5 = the same topic).

A candidate about a different topic, or one the user's message does not speak
to (for example: the user says where THEY are from, but the candidate is about
the BOT's location) scores low on all three scales.

Each candidate carries a `similarity score`: an integer from 0 to 1000 (higher
means a closer semantic match; 1000 is an exact match). Treat it as a hint, not
a hard threshold — a high score still has to be on-topic to score high.

You do not decide what is kept or dropped — that decision is made downstream
from your scores. Score every listed candidate; omit none; do not invent ids.

Return exactly one JSON object with two fields, in this order:
- `reasoning`: first, 1-3 short sentences calibrating yourself — does any
  candidate genuinely match the user's message, and why or why not.
- `items`: then a list with one entry per listed candidate:
  {"id": "<candidate id>", "direct": 1..5, "indirect": 1..5,
   "relevancy": 1..5}

Output only the JSON object. No prose outside it, no markdown fences."""


TOP_K_FILTER: int = 5

# Code-side keep/drop policy over the LLM's scores (docs in apply_filter_scores).
# THRESHOLD: a scale value that marks a candidate clearly relevant on its own.
# TOP_N/TOP_FLOOR: the best-ranked TOP_N candidates are kept on relative merit,
# unless even their best scale sits below TOP_FLOOR (pure noise). The rank rule
# exists because Likert calibration varies wildly between scorer models — a
# conservative model scoring its best candidate 2/1/3 must not empty the list.
FILTER_KEEP_THRESHOLD: int = 4
FILTER_KEEP_TOP_N: int = 2
FILTER_KEEP_TOP_FLOOR: int = 2


@dataclass
class ScoredCandidate:
    """One candidate after the code-side keep/drop decision: the LLM's three
    scores (0 = the LLM omitted this candidate) plus the verdict."""

    qa_id: str
    direct: int
    indirect: int
    relevancy: int
    kept: bool
    # Set only by the assistant's reranker backend (agents/recall_reranker.py),
    # which scores relevance as one number and leaves the three Likert scales
    # at 0. None means an LLM produced this row, and the scales are the scores.
    rerank_score: float | None = None


def apply_filter_scores(
    decision: FilterDecision, candidates: list[Match], *, top_k: int = TOP_K_FILTER
) -> list[ScoredCandidate]:
    """The keep/drop decision, in code — the LLM only supplies scores.

    Policy: with fewer than `top_k` candidates there is no real competition, so
    ALL candidates are kept (an over-aggressive scorer can no longer empty a
    small result set). With a full list the decision is RELATIVE first,
    absolute second: candidates are ranked best-first, the top
    FILTER_KEEP_TOP_N survive on rank alone (unless even their best scale is
    below FILTER_KEEP_TOP_FLOOR — pure noise stays droppable), and any
    lower-ranked candidate with a scale at FILTER_KEEP_THRESHOLD is kept too.
    A ranked policy can't be emptied by a scorer model that calibrates the
    whole scale low. Score rows for ids not in `candidates` (hallucinated) are
    ignored; candidates the LLM did not score default to 0/0/0 (dropped on a
    full list, kept on a small one). Returns every candidate ordered
    best-first (direct, then indirect, then relevancy, then semantic rank)."""
    candidate_ids = {c.qa_id for c in candidates}
    by_id: dict[str, FilterScore] = {}
    for item in decision.items:
        if item.id in candidate_ids and item.id not in by_id:
            by_id[item.id] = item
    keep_all = len(candidates) < top_k
    scored: list[ScoredCandidate] = []
    for c in candidates:
        item = by_id.get(c.qa_id)
        d, i, r = ((item.direct, item.indirect, item.relevancy)
                   if item is not None else (0, 0, 0))
        scored.append(ScoredCandidate(
            qa_id=c.qa_id, direct=d, indirect=i, relevancy=r, kept=keep_all))
    # Stable sort: ties keep the semantic ranking order of `candidates`.
    scored.sort(key=lambda s: (-s.direct, -s.indirect, -s.relevancy))
    if not keep_all:
        for rank, s in enumerate(scored):
            best_scale = max(s.direct, s.indirect, s.relevancy)
            s.kept = (best_scale >= FILTER_KEEP_THRESHOLD
                      or (rank < FILTER_KEEP_TOP_N
                          and best_scale >= FILTER_KEEP_TOP_FLOOR))
    return scored


# Field order for candidate rows in the filter prompt; a row renders only the
# fields it carries.
_FILTER_ROW_FIELDS: tuple[str, ...] = (
    "source", "path", "similarity score", "matched_question", "kind",
    "answer", "handler", "text",
)


def build_filter_prompt_rows(query: str, rows: list[dict[str, Any]]) -> str:
    """User prompt for the relevance-filter LLM call from prepared candidate
    rows — each a dict with an `id` plus any of `_FILTER_ROW_FIELDS`. The
    generic shape lets one filter call score mixed candidate kinds (seed Q&A
    entries and memory claims) side by side."""
    lines = [f"Current user message: {query!r}", "", "Candidates:"]
    for row in rows:
        lines.append(f"  - id: {row['id']}")
        for field in _FILTER_ROW_FIELDS:
            value = row.get(field)
            if value not in (None, ""):
                lines.append(f"    {field}: {value}")
        lines.append("")
    return "\n".join(lines)


def seed_candidate_rows(candidates: list[Match]) -> list[dict[str, Any]]:
    """Filter-prompt rows for seed KB candidates: qa_id/path/score/question
    plus the answer (static) or handler name (dynamic)."""
    rows: list[dict[str, Any]] = []
    for c in candidates:
        entry = get_entry(c.qa_id) or {}
        kind = entry.get("kind", "?")
        row: dict[str, Any] = {
            "id": c.qa_id,
            "path": entry.get("path"),
            "similarity score": score_permille(c.score),
            "matched_question": repr(c.matched_question),
            "kind": kind,
        }
        if kind == "static":
            row["answer"] = repr(entry.get("answer", ""))
        elif kind == "dynamic":
            row["handler"] = entry.get("handler", "")
        rows.append(row)
    return rows
