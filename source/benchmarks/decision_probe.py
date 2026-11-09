"""Opt-in benchmark: System One decision models on repeat and verdict checks.

Measures the checks proposed in notes/proposals/2026-11-09-noticing-a-repeated-
request.md and 2026-11-09-later-verdicts-on-earlier-messages.md, on the
hand-written cases in benchmarks/fixtures/decision_probe_cases.json:

- identity_pair / identity_fanout: do two requests name the same task?
- redo_noul: the yes/no redo framing that short_message_choice replaced;
- short_message_choice: what a short message does (redo, expand, command, ...);
- request_classification: what a request needs (compute, a lookup, an
  action, ...) and whether it needs exact computation, states constraints, or
  is ambiguous — the check that would run before acceptance criteria;
- reply_checklist: did a reply answer the request and its constraints;
- verdict_choice / hedge / same_answer: later verdicts on earlier messages.

It cannot be a unit test: the answers depend on the model, its digest and the
Ollama build. It is run deliberately, against a local Ollama server:

    python -m benchmarks.decision_probe d1:3b d3:4b
    python -m benchmarks.decision_probe d1:3b --out /tmp/probe.json --cold

Writes one JSON document per run: server version, model digests, every raw
answer with its timing, and per-experiment counts — top-answer accuracy, how
many answers cleared the bands, how many of those were wrong, and abstentions.
Touches no database. Refuses `:cloud` models and non-loopback servers, since a
local server relays `:cloud` names to a remote one.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CASES_PATH = Path(__file__).parent / "fixtures" / "decision_probe_cases.json"
DEFAULT_URL = "http://localhost:11434"
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def load_cases(path: Path = CASES_PATH) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# --- request building (pure; what the fixture's experiments send) ----------

def build_requests(exp: dict[str, Any]) -> list[dict[str, Any]]:
    """Every request an experiment sends, as {case, context, state, questions,
    expected}. One request per case (per case and context for a Choice with
    contexts); one request in total for a fan-out."""
    kind = exp["kind"]
    out: list[dict[str, Any]] = []
    if kind == "noul":
        q = {"type": "noul", "instructions": exp["question"]}
        for c in exp["cases"]:
            out.append({"case": c["id"], "context": None, "state": c["state"],
                        "questions": {"q": q}, "expected": {"q": c["expected"]}})
    elif kind == "choice":
        q = {"type": "choice", "instructions": exp["question"],
             "criteria": exp["options"]}
        for ctx in exp.get("contexts") or [None]:
            for c in exp["cases"]:
                if ctx is None:
                    state = c["state"]
                elif ctx["previous_request"] is None:
                    state = {"short_message": c["short_message"]}
                else:
                    state = {"previous_request": ctx["previous_request"],
                             "short_message": c["short_message"]}
                out.append({"case": c["id"], "context": ctx and ctx["id"],
                            "state": state, "questions": {"q": q},
                            "expected": {"q": c["expected"]}})
    elif kind == "noul_checklist":
        only_with_earlier = set(exp["with_earlier_only"])
        for c in exp["cases"]:
            names = [n for n in exp["questions"]
                     if "earlier_replies" in c["state"] or n not in only_with_earlier]
            out.append({"case": c["id"], "context": None, "state": c["state"],
                        "questions": {n: {"type": "noul",
                                          "instructions": exp["questions"][n]}
                                      for n in names},
                        "expected": {n: v for n, v in c["expected"].items()
                                     if n in names}})
    elif kind == "checklist":
        # Typed questions (Choice and Noul mixed) asked together of one state.
        for c in exp["cases"]:
            out.append({"case": c["id"], "context": None, "state": c["state"],
                        "questions": exp["questions"],
                        "expected": c["expected"]})
    elif kind == "noul_fanout":
        out.append({
            "case": "fanout", "context": None,
            "state": {"current_message": exp["current_message"]},
            "questions": {c["id"]: {"type": "noul", "instructions": {
                "question": exp["question"],
                "earlier_message": c["earlier_message"]}}
                for c in exp["candidates"]},
            "expected": {c["id"]: c["expected"] for c in exp["candidates"]},
        })
    else:
        raise ValueError(f"unknown experiment kind {kind!r}")
    return out


# --- scoring (pure) ---------------------------------------------------------

def score_noul(p: float, expected: bool, bands: dict[str, float]) -> dict[str, Any]:
    """Top answer at 0.5, and the banded verdict: yes / no / unclear."""
    band = "yes" if p >= bands["yes"] else "no" if p <= bands["no"] else "unclear"
    accepted = band != "unclear"
    return {"p": p, "top_ok": (p >= 0.5) == expected, "band": band,
            "accepted": accepted,
            "accepted_wrong": accepted and ((band == "yes") != expected)}


def score_choice(probabilities: dict[str, float], expected: str,
                 accept: float) -> dict[str, Any]:
    top = max(probabilities, key=lambda k: probabilities[k])
    p = probabilities[top]
    accepted = p >= accept
    return {"choice": top, "p": p, "top_ok": top == expected,
            "accepted": accepted, "accepted_wrong": accepted and top != expected}


def score_answers(answers: dict[str, Any], req: dict[str, Any],
                  bands: dict[str, float]) -> dict[str, Any]:
    """Score one response against the request's expected labels. A question
    with no expected label is recorded but not scored."""
    scored = {}
    for name, ans in answers.items():
        if name not in req["expected"]:
            continue
        exp = req["expected"][name]
        if ans.get("type") == "choice":
            scored[name] = score_choice(ans["probabilities"], exp, bands["choice_accept"])
        else:
            scored[name] = score_noul(ans["noul"], exp, bands)
    return scored


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Per question name across an experiment's requests: scored, top-answer
    right, accepted in band, accepted wrong, abstained; plus median latency."""
    per: dict[str, dict[str, int]] = {}
    for r in results:
        for name, s in r.get("scored", {}).items():
            t = per.setdefault(name, {"scored": 0, "top_ok": 0, "accepted": 0,
                                      "accepted_wrong": 0, "abstained": 0})
            t["scored"] += 1
            t["top_ok"] += s["top_ok"]
            t["accepted"] += s["accepted"]
            t["accepted_wrong"] += s["accepted_wrong"]
            t["abstained"] += not s["accepted"]
    ms = [r["ms"] for r in results if "ms" in r]
    errors = sum(1 for r in results if "error" in r)
    return {"by_question": per, "requests": len(results), "errors": errors,
            "median_ms": round(statistics.median(ms)) if ms else None}


# --- talking to Ollama ------------------------------------------------------

def check_local(base_url: str, model: str) -> None:
    host = urllib.parse.urlparse(base_url).hostname
    if host not in LOOPBACK_HOSTS:
        raise SystemExit(f"refusing non-loopback server {base_url!r}")
    if model.endswith(":cloud") or "-cloud" in model:
        raise SystemExit(f"refusing cloud-backed model {model!r}")


def _http(base_url: str, path: str, body: dict | None = None,
          timeout: float = 180) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base_url + path, data,
                                 {"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def systemone(base_url: str, model: str, state: Any,
              questions: dict[str, Any]) -> tuple[dict[str, Any], float]:
    t = time.monotonic()
    out = _http(base_url, "/v1/systemone",
                {"model": model, "state": state, "questions": questions})
    return out, (time.monotonic() - t) * 1000


def environment(base_url: str, models: list[str]) -> dict[str, Any]:
    tags = {m["name"]: m for m in _http(base_url, "/api/tags")["models"]}
    return {
        "server_version": _http(base_url, "/api/version").get("version"),
        "models": {m: {"digest": tags.get(m, {}).get("digest"),
                       "size": tags.get(m, {}).get("size")} for m in models},
    }


def run_model(base_url: str, model: str, cases: dict[str, Any],
              cold: bool, only: set[str] | None = None) -> dict[str, Any]:
    check_local(base_url, model)
    first: dict[str, Any] = {"cold_requested": cold}
    if cold:
        try:
            _http(base_url, "/api/generate", {"model": model, "keep_alive": 0})
        except Exception as e:  # unloading is best-effort
            first["unload_error"] = str(e)
    warm_q = {"q": {"type": "noul", "instructions": "Is this text in English?"}}
    _, first["first_call_ms"] = systemone(base_url, model, "warm up", warm_q)
    first["first_call_ms"] = round(first["first_call_ms"])

    experiments = {}
    for exp in cases["experiments"]:
        if only and exp["id"] not in only:
            continue
        results = []
        for req in build_requests(exp):
            row: dict[str, Any] = {"case": req["case"], "context": req["context"]}
            try:
                out, ms = systemone(base_url, model, req["state"], req["questions"])
                row["ms"] = round(ms)
                row["answers"] = out.get("answers", {})
                row["scored"] = score_answers(row["answers"], req, cases["bands"])
            except Exception as e:
                row["error"] = str(e)
            results.append(row)
        experiments[exp["id"]] = {"results": results, "summary": summarize(results)}
    return {"model": model, "first_call": first, "experiments": experiments}


def print_summary(run: dict[str, Any]) -> None:
    print(f"\n=== {run['model']}  (first call {run['first_call']['first_call_ms']} ms)")
    for exp_id, e in run["experiments"].items():
        s = e["summary"]
        print(f"  {exp_id}: {s['requests']} requests, median {s['median_ms']} ms"
              + (f", {s['errors']} errors" if s["errors"] else ""))
        for name, t in s["by_question"].items():
            label = "" if name == "q" else f"{name}: "
            print(f"    {label}top {t['top_ok']}/{t['scored']}, "
                  f"in band {t['accepted']} ({t['accepted_wrong']} wrong), "
                  f"abstained {t['abstained']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("models", nargs="+", help="decision models, e.g. d1:3b d3:4b")
    ap.add_argument("--out", type=Path, help="write the JSON run document here")
    ap.add_argument("--cold", action="store_true",
                    help="unload each model first, so first_call_ms is a cold load")
    ap.add_argument("--only", nargs="+", metavar="EXPERIMENT",
                    help="run only these experiment ids")
    ap.add_argument("--url", default=os.environ.get("DECISION_PROBE_URL", DEFAULT_URL))
    args = ap.parse_args(argv)

    cases = load_cases()
    for m in args.models:
        check_local(args.url, m)
    doc = {
        "fixture_version": cases["version"],
        "only": args.only,
        "bands": cases["bands"],
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "environment": environment(args.url, args.models),
        "runs": [],
    }
    for m in args.models:
        run = run_model(args.url, m, cases, args.cold,
                        set(args.only) if args.only else None)
        print_summary(run)
        doc["runs"].append(run)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n",
                            encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
