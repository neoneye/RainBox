"""The decision models: typed questions scored by a local model.

The only code that talks to `/v1/systemone` on the local Ollama server. A
request is `{model, state, questions}`. Each question is answered alone,
against the shared state, with a probability and no generated text: a Noul
question gets P(yes), a Choice question a probability per option.

`ask` never raises for a model problem. A failure is an `Answer` whose
status is `unknown` and whose `reason` says why, so a caller has one code
path and falls back to whatever it did without the model. Every `ask`
writes one `decision_call` row: the questions as they were worded, the raw
probabilities and the banded labels, and a digest of the state, never the
state's text. The state is the operator's messages, which have their own
rows: a page that shows a call rebuilds the state from them and finds the
call by the digest (`state_digest`).

A decision slot does not fall back to `assistant.default`: a generative
model cannot answer this API. Design:
notes/proposals/2026-11-09-decision-model-foundation.md.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

logger = logging.getLogger(__name__)

#: Bumped when the default bands change.
BANDS_VERSION = 1
#: How long a model's locality check is trusted before it is made again.
LOCALITY_TTL_SECONDS = 300.0


@dataclass(frozen=True)
class Bands:
    """Where a probability becomes a label. A Noul is `yes` at or above
    `yes`, `no` at or below `no`, and `unclear` between them. A Choice's top
    option is accepted at or above `choice`."""

    yes: float = 0.8
    no: float = 0.2
    choice: float = 0.8


DEFAULT_BANDS = Bands()


@dataclass(frozen=True)
class Question:
    name: str
    kind: Literal["noul", "choice"]
    #: Code-owned and versioned. No example phrases: the model scores the
    #: state against these words.
    instructions: str
    version: int = 1
    #: Choice only: option name -> description.
    options: dict[str, str] | None = None
    #: This question's own bands, when the defaults do not fit it.
    bands: Bands | None = None


@dataclass(frozen=True)
class Answer:
    name: str
    status: Literal["ok", "unknown"]
    #: Noul: P(yes). Choice: P(top option).
    p: float | None = None
    #: Noul: "yes", "no" or "unclear". Choice: the top option, or None when
    #: it is below its band.
    label: str | None = None
    probabilities: dict[str, float] | None = None
    #: Why unknown: unbound, not_local, oversized, timeout, error, invalid.
    reason: str | None = None

    @property
    def yes(self) -> bool:
        return self.status == "ok" and self.label == "yes"


class _NotLocal(Exception):
    pass


def _http(base_url: str, path: str, body: dict[str, Any] | None,
          timeout: float) -> dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base_url.rstrip("/") + path, data, {"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


_locality: dict[tuple[str, str], tuple[float, str, str]] = {}


def verify_local(base_url: str, model: str, timeout: float = 5.0) -> tuple[str, str]:
    """`(model_digest, server_version)` for a model whose weights are on
    this machine; raises `_NotLocal` otherwise. A loopback address is
    necessary and not sufficient: a local server relays a `:cloud` model to
    a remote one. So the name must not say cloud, the server must list the
    model with a digest and no remote host, and must hold its weights."""
    from diary.embeddings import is_loopback_url

    key = (base_url, model)
    cached = _locality.get(key)
    if cached and time.monotonic() - cached[0] < LOCALITY_TTL_SECONDS:
        return cached[1], cached[2]
    if not is_loopback_url(base_url):
        raise _NotLocal(f"{base_url} is not a loopback address")
    if model.endswith(":cloud") or "-cloud" in model:
        raise _NotLocal(f"{model} is a cloud model")
    tags = {m.get("name"): m for m in _http(base_url, "/api/tags", None, timeout)["models"]}
    listed = tags.get(model)
    if not listed or not listed.get("digest"):
        raise _NotLocal(f"{model} is not among the server's models")
    show = _http(base_url, "/api/show", {"model": model}, timeout)
    if (listed.get("remote_host") or listed.get("remote_model")
            or show.get("remote_host") or show.get("remote_model")
            or not show.get("model_info")):
        raise _NotLocal(f"{model} does not have its weights on this machine")
    version = str(_http(base_url, "/api/version", None, timeout).get("version") or "")
    _locality[key] = (time.monotonic(), listed["digest"], version)
    return listed["digest"], version


def state_digest(state: dict[str, Any]) -> str:
    """What a `decision_call` row keeps of the state it was asked about. A
    page that rebuilds the state from the messages finds its row by this."""
    return hashlib.sha256(json.dumps(
        state, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _valid_p(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and 0.0 <= value <= 1.0)


def _answer(question: Question, raw: Any, bands: Bands) -> Answer:
    """One answer, checked before it is believed."""
    band = question.bands or bands
    if not isinstance(raw, dict):
        return Answer(question.name, "unknown", reason="invalid")
    if question.kind == "noul":
        p = raw.get("noul")
        if not isinstance(p, (int, float)) or not _valid_p(p):
            return Answer(question.name, "unknown", reason="invalid")
        label = "yes" if p >= band.yes else "no" if p <= band.no else "unclear"
        return Answer(question.name, "ok", p=float(p), label=label)
    probabilities = raw.get("probabilities")
    options = question.options or {}
    if (not isinstance(probabilities, dict) or set(probabilities) != set(options)
            or not all(_valid_p(v) for v in probabilities.values())
            or abs(sum(probabilities.values()) - 1.0) > 0.01):
        return Answer(question.name, "unknown", reason="invalid")
    top = max(probabilities, key=lambda k: probabilities[k])
    p = float(probabilities[top])
    return Answer(question.name, "ok", p=p, label=top if p >= band.choice else None,
                  probabilities={k: float(v) for k, v in probabilities.items()})


def _payload(question: Question) -> dict[str, Any]:
    out: dict[str, Any] = {"type": question.kind, "instructions": question.instructions}
    if question.kind == "choice":
        out["options"] = question.options or {}
    return out


def ask_model(base_url: str, model: str, state: dict[str, Any],
              questions: list[Question], *, deadline_s: float,
              max_state_chars: int, bands: Bands = DEFAULT_BANDS,
              ) -> tuple[list[Answer], dict[str, Any]]:
    """One request to one model. Returns the answers and what to record
    about the call: `{model_digest, server_version, state_digest,
    elapsed_ms, reason}`.

    The state is never cut to fit. One longer than `max_state_chars` is not
    sent: a judgement on half of it is worse than none."""
    serialized = json.dumps(state, ensure_ascii=False, sort_keys=True)
    meta: dict[str, Any] = {
        "model_digest": None, "server_version": None, "elapsed_ms": None, "reason": None,
        "state_digest": state_digest(state)}

    def unknown(reason: str) -> tuple[list[Answer], dict[str, Any]]:
        meta["reason"] = reason
        return [Answer(q.name, "unknown", reason=reason) for q in questions], meta

    if len(serialized) > max_state_chars:
        return unknown("oversized")
    started = time.monotonic()
    try:
        meta["model_digest"], meta["server_version"] = verify_local(base_url, model)
        out = _http(base_url, "/v1/systemone", {
            "model": model, "state": state,
            "questions": {q.name: _payload(q) for q in questions}}, deadline_s)
    except _NotLocal as e:
        logger.warning("decision model refused: %s", e)
        return unknown("not_local")
    except TimeoutError:
        return unknown("timeout")
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        timed_out = isinstance(getattr(e, "reason", None), TimeoutError)
        logger.warning("decision call failed: %s", e)
        return unknown("timeout" if timed_out else "error")
    finally:
        meta["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    raw = out.get("answers") if isinstance(out, dict) else None
    if not isinstance(raw, dict) or set(raw) != {q.name for q in questions}:
        return unknown("invalid")
    return [_answer(q, raw[q.name], bands) for q in questions], meta


def slot_model(slot_uuid: UUID) -> tuple[str, str] | None:
    """`(base_url, model_name)` of the one model a decision slot is bound
    to: the first member of its group. None when the slot is unbound. No
    fallback to another slot."""
    import db
    from agents.model_groups import decision_slot_member_uuids, model_base_url

    for member in decision_slot_member_uuids(slot_uuid):
        url = model_base_url(member)
        try:
            _provider, name, _args = db.resolved_model_kwargs(member)
        except LookupError:
            continue
        if url:
            return url, name
    return None


def ask(slot_uuid: UUID, state: dict[str, Any], questions: list[Question], *,
        deadline_s: float, max_state_chars: int, bands: Bands = DEFAULT_BANDS,
        question_set_version: int = 1, run_uuid: UUID | None = None,
        job_uuid: UUID | None = None,
        bound: tuple[str, str] | None = None) -> list[Answer]:
    """One request to the model bound to a decision slot, recorded as one
    `decision_call` row. `bound` is `slot_model(slot_uuid)` resolved earlier
    by a caller that asks several times in one job."""
    import db
    from agents.config import role_name

    target = bound or slot_model(slot_uuid)
    if target is None:
        answers = [Answer(q.name, "unknown", reason="unbound") for q in questions]
        meta: dict[str, Any] = {
            "model_digest": None, "server_version": None, "elapsed_ms": None,
            "reason": "unbound", "state_digest": state_digest(state)}
    else:
        answers, meta = ask_model(
            target[0], target[1], state, questions, deadline_s=deadline_s,
            max_state_chars=max_state_chars, bands=bands)
    try:
        db.record_decision_call(
            run_uuid=run_uuid, job_uuid=job_uuid,
            slot=role_name(slot_uuid) or str(slot_uuid),
            model=target[1] if target else None,
            model_digest=meta["model_digest"], server_version=meta["server_version"],
            question_set_version=question_set_version, bands_version=BANDS_VERSION,
            state_digest=meta["state_digest"],
            questions=[{"name": q.name, "kind": q.kind, "version": q.version,
                        "instructions": q.instructions, "options": q.options}
                       for q in questions],
            answers=[{"name": a.name, "status": a.status, "p": a.p, "label": a.label,
                      "probabilities": a.probabilities, "reason": a.reason,
                      "bands": vars(q.bands or bands)}
                     for q, a in zip(questions, answers)],
            status="ok" if all(a.status == "ok" for a in answers) else "unknown",
            reason=meta["reason"], elapsed_ms=meta["elapsed_ms"],
            deadline_ms=int(deadline_s * 1000))
    except Exception:   # noqa: BLE001 - a lost record must not lose the answer
        logger.exception("decision call was not recorded")
        db.session.rollback()
    return answers
