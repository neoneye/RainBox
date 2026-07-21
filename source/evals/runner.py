"""Eval runner: score active eval cases against the current chat/memory
implementation and persist EvalRun + EvalResult rows.

Deterministic-first: chat_reply cases use `case.input["actual_output"]`
(no live LLM); memory_retrieval cases call
`memory.retrieval.retrieve_memories(...)`. LLM-as-judge lands in a
later work package.

Scoring policy: each configured criterion (must_include, must_include_any,
must_not_include, expected_memories, forbidden_memories, requires_json,
min_words, max_words) contributes a value in [0.0, 1.0]; the final score is
their mean. A case with no configured criteria scores 1.0 with a `warnings`
flag in details. Pass/fail uses `rubric.threshold` (default 0.7).

`must_include_any` is a list of alternative groups — each group is satisfied
by ANY of its substrings ("mi" or "miles"), so a case can require a unit or
currency label without pinning one spelling. `min_words`/`max_words` are the
deterministic proxy for explanation depth: a teach-depth answer must carry at
least `min_words`, a concise answer at most `max_words`.
"""

import argparse
import json
import logging
import sys
from typing import Any
from uuid import UUID

import db
from db import EvalCase, EvalResult, EvalRun
from memory.retrieval import retrieve_memories

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD: float = 0.7

SUPPORTED_CONFIG_KNOBS: set[str] = {
    "memory_retrieval_limit",
    "memory_include_secret",
}


# --- scoring helpers --------------------------------------------------------


def _score_must_include(output_text: str, items: list[str]) -> tuple[float, dict[str, Any]]:
    if not items:
        return 1.0, {"matched": 0, "total": 0, "skipped": True}
    matched = sum(1 for s in items if s in output_text)
    return matched / len(items), {"matched": matched, "total": len(items)}


def _score_must_include_any(
    output_text: str, groups: list[Any],
) -> tuple[float, dict[str, Any]]:
    """Each group is a list of alternative substrings; a group counts as
    matched when ANY alternative is present. BINARY: the criterion scores 1.0
    only when EVERY group matched — a case demanding both a unit label and a
    currency label must not pass with one of the two, and fractional credit
    averaged into the mean would allow exactly that."""
    valid = [g for g in groups if isinstance(g, list) and g]
    if not valid:
        return 1.0, {"matched": 0, "total": 0, "skipped": True}
    matched = sum(1 for g in valid
                  if any(str(alt) in output_text for alt in g))
    return (1.0 if matched == len(valid) else 0.0), {
        "matched": matched, "total": len(valid)}


def _score_word_bounds(
    output_text: str, min_words: Any, max_words: Any,
) -> tuple[float, dict[str, Any]]:
    """Binary length criterion: 1.0 when the word count sits inside the
    configured bounds (either side optional). The deterministic proxy for
    explanation depth — teach answers must reach min_words, concise answers
    must stay under max_words."""
    if min_words is None and max_words is None:
        return 1.0, {"skipped": True}
    count = len(output_text.split())
    ok = ((min_words is None or count >= int(min_words))
          and (max_words is None or count <= int(max_words)))
    return (1.0 if ok else 0.0), {
        "words": count, "min_words": min_words, "max_words": max_words}


def _score_must_not_include(output_text: str, items: list[str]) -> tuple[float, dict[str, Any]]:
    if not items:
        return 1.0, {"absent": 0, "total": 0, "skipped": True}
    absent = sum(1 for s in items if s not in output_text)
    return absent / len(items), {"absent": absent, "total": len(items)}


def _score_expected_memories(
    expected: list[str], retrieved: list[Any],
) -> tuple[float, dict[str, Any]]:
    if not expected:
        return 1.0, {"matched": 0, "total": 0, "skipped": True}
    haystack: list[str] = []
    for m in retrieved:
        muuid = getattr(m, "uuid", None)
        if muuid is not None:
            haystack.append(str(muuid))
        text = getattr(m, "text", None)
        if text:
            haystack.append(text)
    # Use substring matching: an expected entry is "present" if it appears as
    # an exact match OR as a substring of any haystack entry.
    def _present(e: str) -> bool:
        return any(e in h for h in haystack)
    matched = sum(1 for e in expected if _present(e))
    return matched / len(expected), {"matched": matched, "total": len(expected)}


def _score_forbidden_memories(
    forbidden: list[str], retrieved: list[Any],
) -> tuple[float, dict[str, Any]]:
    if not forbidden:
        return 1.0, {"absent": 0, "total": 0, "skipped": True}
    haystack: list[str] = []
    for m in retrieved:
        muuid = getattr(m, "uuid", None)
        if muuid is not None:
            haystack.append(str(muuid))
        text = getattr(m, "text", None)
        if text:
            haystack.append(text)
    # Use substring matching: a forbidden entry is "present" if it appears as
    # an exact match OR as a substring of any haystack entry.
    def _present(f: str) -> bool:
        return any(f in h for h in haystack)
    absent = sum(1 for f in forbidden if not _present(f))
    return absent / len(forbidden), {"absent": absent, "total": len(forbidden)}


def _aggregate(parts: list[tuple[float, dict[str, Any]]]) -> tuple[float, dict[str, Any]]:
    """Mean of the values across all criteria that actually ran (i.e.,
    are not flagged `skipped`)."""
    active = [(v, d) for (v, d) in parts if not d.get("skipped")]
    details = {("crit_%d" % i): d for i, (_v, d) in enumerate(parts)}
    if not active:
        return 1.0, {**details, "warnings": ["no criteria configured"]}
    score = sum(v for v, _d in active) / len(active)
    return score, details


# --- public scoring API -----------------------------------------------------


def score_chat_reply_case(
    case: EvalCase, output: dict[str, Any],
) -> tuple[float, dict[str, Any]]:
    """Score a chat_reply case against `output["text"]`. The output dict
    is intentionally a dict (not a bare string) so future versions can
    pass structured info."""
    text = str(output.get("text") or "")
    expected = case.expected or {}
    parts: list[tuple[float, dict[str, Any]]] = []
    detail: dict[str, Any] = {}

    s, d = _score_must_include(text, list(expected.get("must_include") or []))
    parts.append((s, d)); detail["must_include"] = d

    s, d = _score_must_include_any(text, list(expected.get("must_include_any") or []))
    parts.append((s, d)); detail["must_include_any"] = d

    s, d = _score_must_not_include(text, list(expected.get("must_not_include") or []))
    parts.append((s, d)); detail["must_not_include"] = d

    s, d = _score_word_bounds(text, expected.get("min_words"),
                              expected.get("max_words"))
    parts.append((s, d)); detail["word_bounds"] = d

    # `expected_memories` / `forbidden_memories` apply to chat_reply too:
    # the case can include retrieved snapshots if it carries the debug-memory
    # payload. For v1 we score over the rated reply's own text — i.e., the
    # memory uuid/text must appear in the actual_output text.
    s, d = _score_expected_memories(
        list(expected.get("expected_memories") or []),
        [type("M", (), {"text": text})()],
    )
    parts.append((s, d)); detail["expected_memories"] = d

    s, d = _score_forbidden_memories(
        list(expected.get("forbidden_memories") or []),
        [type("M", (), {"text": text})()],
    )
    parts.append((s, d)); detail["forbidden_memories"] = d

    if expected.get("requires_json"):
        try:
            json.loads(text)
            s, d = 1.0, {"valid_json": True}
        except (ValueError, TypeError):
            s, d = 0.0, {"valid_json": False}
        parts.append((s, d)); detail["requires_json"] = d

    score, agg_detail = _aggregate(parts)
    if "warnings" in agg_detail:
        detail["warnings"] = agg_detail["warnings"]
    return score, detail


def score_memory_retrieval_case(
    case: EvalCase, retrieved: list[Any],
) -> tuple[float, dict[str, Any]]:
    """Score a memory_retrieval case against the retrieved memories
    (list of RetrievedMemory-like objects with .uuid and .text)."""
    expected = case.expected or {}
    parts: list[tuple[float, dict[str, Any]]] = []
    detail: dict[str, Any] = {}

    s, d = _score_expected_memories(
        list(expected.get("expected_memories") or []), retrieved,
    )
    parts.append((s, d)); detail["expected_memories"] = d

    s, d = _score_forbidden_memories(
        list(expected.get("forbidden_memories") or []), retrieved,
    )
    parts.append((s, d)); detail["forbidden_memories"] = d

    score, agg_detail = _aggregate(parts)
    if "warnings" in agg_detail:
        detail["warnings"] = agg_detail["warnings"]
    return score, detail


# --- runner -----------------------------------------------------------------


def _threshold(case: EvalCase) -> float:
    rubric = case.rubric or {}
    try:
        return float(rubric.get("threshold", DEFAULT_THRESHOLD))
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLD


def run_eval_case(
    case: EvalCase,
    *,
    eval_run_uuid: UUID | None = None,
    config: dict[str, Any] | None = None,
) -> EvalResult:
    """Score one EvalCase and persist an EvalResult. The caller passes
    `eval_run_uuid` to attach it to a parent run (typical when called by
    `run_eval_suite`); ad-hoc invocations may create a one-off run and
    pass it in. `config` is the optimizer candidate config — currently
    threads `memory_retrieval_limit` and `memory_include_secret`
    into retrieve_memories. When None, behavior matches
    retrieve_memories's own defaults."""
    if eval_run_uuid is None:
        run = db.create_eval_run(name=f"ad-hoc: {case.name}", agent_role="chat")
        eval_run_uuid = run.uuid

    if case.case_type == "chat_reply":
        actual = (case.input or {}).get("actual_output", "")
        score, details = score_chat_reply_case(case, {"text": actual})
    elif case.case_type == "memory_retrieval":
        case_input = case.input or {}
        limit = (config or {}).get("memory_retrieval_limit", 6)
        include_secret = (config or {}).get("memory_include_secret", False)
        retrieved = retrieve_memories(
            case_input.get("query", ""),
            agent_uuid=_optional_uuid(case_input.get("agent_uuid")),
            room_uuid=_optional_uuid(case_input.get("room_uuid")),
            limit=limit,
            include_secret=include_secret,
        )
        score, details = score_memory_retrieval_case(case, retrieved)
    else:
        score, details = 0.0, {"error": f"unsupported case_type: {case.case_type}"}

    threshold = _threshold(case)
    return db.create_eval_result(
        eval_run_uuid=eval_run_uuid,
        eval_case_uuid=case.uuid,
        score=score,
        passed=score >= threshold,
        details={"threshold": threshold, **details},
    )


def _optional_uuid(value: Any) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, TypeError):
        return None


def run_eval_suite(
    case_uuids: list[UUID] | None = None,
    *,
    name: str = "",
    split: str | None = None,
    config: dict[str, Any] | None = None,
    case_filter: dict[str, Any] | None = None,
    agent_role: str = "chat",
) -> EvalRun:
    """Run a set of eval cases. If `case_uuids` is None, run every
    active case (optionally filtered by `split`). Persists one EvalRun
    plus one EvalResult per case, stamps `finished_at` + a summary blob.

    `config` is an optional optimizer candidate config; supported knobs
    (see SUPPORTED_CONFIG_KNOBS) flow through to retrieve_memories.
    Unsupported keys are recorded under `unsupported_config_keys` on
    the persisted EvalRun.config so callers can tell the runner didn't
    silently evaluate them. `case_filter` is an alternative way to
    select cases (supports `case_uuids` and `split` keys) — accepted
    so the optimizer's default runner can thread its filter through.
    """
    if case_uuids is None and case_filter:
        if case_filter.get("case_uuids"):
            case_uuids = list(case_filter["case_uuids"])
        elif case_filter.get("split"):
            split = case_filter["split"]

    if case_uuids is None:
        cases = db.list_eval_cases(status="active", split=split)
    else:
        cases = [c for c in (db.get_eval_case(u) for u in case_uuids) if c is not None]

    candidate_config = dict(config or {})
    unsupported = sorted(
        k for k in candidate_config.keys() if k not in SUPPORTED_CONFIG_KNOBS
    )
    persist_config: dict[str, Any] = {
        **candidate_config,
        "case_uuids": [str(u) for u in case_uuids] if case_uuids else None,
        "split": split,
    }
    if unsupported:
        persist_config["unsupported_config_keys"] = unsupported

    run = db.create_eval_run(
        name=name or "eval suite",
        agent_role=agent_role,
        config=persist_config,
    )
    for case in cases:
        run_eval_case(case, eval_run_uuid=run.uuid, config=candidate_config)

    results = db.list_eval_results_for_run(run.uuid)
    passed = sum(1 for r in results if r.passed)
    mean_score = sum(r.score for r in results) / len(results) if results else 0.0
    case_names = {c.uuid: c.name for c in cases}
    summary = {
        "cases": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "mean_score": mean_score,
        "failures": [
            {
                "eval_case_uuid": str(r.eval_case_uuid),
                "eval_case_name": case_names.get(r.eval_case_uuid, ""),
                "score": r.score,
                "details": r.details,
            }
            for r in results if not r.passed
        ],
    }
    return db.finish_eval_run(run.uuid, summary=summary)


# --- CLI --------------------------------------------------------------------


def _print_summary(run: EvalRun) -> None:
    s = run.summary or {}
    print(f"Eval run {run.uuid}")
    print(f"Cases: {s.get('cases', 0)}")
    print(f"Passed: {s.get('passed', 0)}")
    if "mean_score" in s:
        print(f"Mean score: {s['mean_score']:.2f}")
    failures = s.get("failures") or []
    if failures:
        print("Failures:")
        for f in failures:
            label = f.get("eval_case_name") or f.get("eval_case_uuid")
            score = f.get("score", 0.0)
            reasons: list[str] = []
            for key in ("must_include", "must_not_include",
                        "expected_memories", "forbidden_memories"):
                d = (f.get("details") or {}).get(key) or {}
                if d.get("total", 0) > 0 and (
                    d.get("matched", d.get("absent", 0)) < d.get("total", 0)
                ):
                    reasons.append(key)
            req_json = (f.get("details") or {}).get("requires_json") or {}
            if req_json.get("valid_json") is False:
                reasons.append("requires_json")
            reason = ", ".join(reasons) or "below threshold"
            print(f"- {label} score={score:.2f} reason={reason}")


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="evals.runner",
        description="Run active eval cases against the current chat/memory implementation.",
    )
    parser.add_argument(
        "--active", action="store_true",
        help="Run every active eval case (no case filter).",
    )
    parser.add_argument(
        "--case", action="append", default=[],
        help="Run a specific case by uuid. May be repeated.",
    )
    parser.add_argument(
        "--split", default=None, choices=["train", "holdout", "regression"],
        help="When using --active, restrict by split.",
    )
    parser.add_argument(
        "--name", default="", help="Optional name for the EvalRun row.",
    )
    args = parser.parse_args(argv)

    if not args.active and not args.case:
        parser.error("must pass --active or at least one --case")

    app = db.make_app()
    db.init_db(app)
    with app.app_context():
        case_uuids: list[UUID] | None
        if args.case:
            case_uuids = []
            for raw in args.case:
                try:
                    case_uuids.append(UUID(raw))
                except (ValueError, TypeError):
                    parser.error(f"invalid case uuid: {raw}")
        else:
            case_uuids = None  # --active path
        run = run_eval_suite(
            case_uuids=case_uuids, name=args.name, split=args.split,
        )
        _print_summary(run)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
