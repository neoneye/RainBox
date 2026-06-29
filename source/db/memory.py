"""Memory persistence: memory claims and their evidence.

Split out of db.py. Holds the memory claim/evidence operations
(create_memory_claim, add_memory_evidence, list_memory_claims, supersede_memory,
reject_memory, ...). Re-exported from db for import compatibility.
"""
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import hashlib as _hashlib
import sqlalchemy as sa

from db.models import MemoryClaim, MemoryEmbedding, MemoryEvidence, MemoryRejectedValue, db


def create_memory_claim(
    *,
    scope: str,
    kind: str,
    text: str,
    confidence: float,
    status: str = "candidate",
    sensitivity: str = "private",
    agent_uuid: UUID | None = None,
    room_uuid: UUID | None = None,
    subject: str | None = None,
    predicate: str | None = None,
    object: str | None = None,
    supersedes_uuid: UUID | None = None,
    expires_at: datetime | None = None,
    support_count: int | None = None,
    epistemic_confidence: float | None = None,
    retrieval_strength: float | None = None,
    conflicts_with_uuid: UUID | None = None,
    subj_pred_key: str | None = None,
    value_key: str | None = None,
    key_version: int | None = None,
    commit: bool = True,
) -> MemoryClaim:
    """Insert a memory_claim row. Defaults: status=candidate, sensitivity=private.
    With commit=False the row is flushed (uuid assigned) but not committed, so a
    caller (record_belief) can compose several writes in one transaction."""
    claim = MemoryClaim(
        scope=scope, kind=kind, text=text, confidence=confidence,
        status=status, sensitivity=sensitivity,
        agent_uuid=agent_uuid, room_uuid=room_uuid,
        subject=subject, predicate=predicate, object=object,
        supersedes_uuid=supersedes_uuid, expires_at=expires_at,
        support_count=support_count, epistemic_confidence=epistemic_confidence,
        retrieval_strength=retrieval_strength, conflicts_with_uuid=conflicts_with_uuid,
        subj_pred_key=subj_pred_key, value_key=value_key, key_version=key_version,
    )
    db.session.add(claim)
    db.session.flush()
    if commit:
        db.session.commit()
    return claim


def get_memory_claim(memory_uuid: UUID) -> "MemoryClaim | None":
    """Fetch a memory claim by uuid, or None if not present."""
    return db.session.query(MemoryClaim).filter_by(uuid=memory_uuid).first()


def add_memory_evidence(
    *,
    memory_uuid: UUID,
    provenance: str,
    source_type: str,
    source_id: str | None = None,
    excerpt: str | None = None,
    created_by_uuid: UUID | None = None,
    commit: bool = True,
) -> MemoryEvidence:
    """Attach a provenance row to a memory claim. Returns the persisted
    evidence with id/uuid/created_at populated."""
    ev = MemoryEvidence(
        memory_uuid=memory_uuid,
        provenance=provenance,
        source_type=source_type,
        source_id=source_id,
        excerpt=excerpt,
        created_by_uuid=created_by_uuid,
    )
    db.session.add(ev)
    db.session.flush()
    if commit:
        db.session.commit()
    return ev


def list_memory_claims(
    *,
    scope: str | None = None,
    agent_uuid: UUID | None = None,
    room_uuid: UUID | None = None,
    status: str | None = None,
    kind: str | None = None,
) -> list[MemoryClaim]:
    """Return claims matching every supplied filter (AND-ed). All filters
    are optional; passing none returns every claim."""
    q = db.session.query(MemoryClaim)
    if scope is not None:
        q = q.filter(MemoryClaim.scope == scope)
    if agent_uuid is not None:
        q = q.filter(MemoryClaim.agent_uuid == agent_uuid)
    if room_uuid is not None:
        q = q.filter(MemoryClaim.room_uuid == room_uuid)
    if status is not None:
        q = q.filter(MemoryClaim.status == status)
    if kind is not None:
        q = q.filter(MemoryClaim.kind == kind)
    return q.order_by(MemoryClaim.id.asc()).all()


def normalize_claim_text(text: str) -> str:
    """Canonical form for duplicate detection: lowercased, leading/trailing and
    internal whitespace collapsed. Two claims whose text normalizes to the same
    string are treated as the same belief."""
    return " ".join((text or "").split()).casefold()


import re as _re

KEY_VERSION = 1
_KEY_SEP = "\x1f"

# (regex over the *normalized* text -> canonical predicate). First match wins.
# Each regex has named groups `s` (subject) and `o` (object/value).
_SHAPE_RULES: tuple[tuple[_re.Pattern, str], ...] = (
    (_re.compile(r"^(?P<s>.+?) is a (?P<o>.+)$"), "is"),
    (_re.compile(r"^(?P<s>.+?) is (?P<o>.+)$"), "is"),
    (_re.compile(r"^(?P<s>.+?) prefers (?P<o>.+)$"), "prefers"),
    (_re.compile(r"^(?P<s>.+?) likes (?P<o>.+)$"), "likes"),
    (_re.compile(r"^(?P<s>.+?) uses (?P<o>.+)$"), "uses"),
    (_re.compile(r"^(?P<s>.+?) works with (?P<o>.+)$"), "uses"),
)


def belief_keys(
    subject: str | None, predicate: str | None,
    object: str | None, text: str,
) -> tuple[str, str]:
    """Return (subj_pred_key, value_key) for conflict/tombstone matching.

    If the caller supplied subject+predicate, key on those. Otherwise run a
    deterministic parser over `text` for a few common shapes; on a match key on
    (subject, predicate)+object. No match -> ("", normalized text). Pure string
    work — no model call. See KEY_VERSION."""
    if subject and predicate:
        sp = normalize_claim_text(subject) + _KEY_SEP + normalize_claim_text(predicate)
        return sp, normalize_claim_text(object or text)
    norm = normalize_claim_text(text)
    for pattern, pred in _SHAPE_RULES:
        m = pattern.match(norm)
        if m:
            return (m.group("s") + _KEY_SEP + pred), m.group("o")
    return "", norm


def find_equivalent_claim(
    text: str,
    *,
    scope: str,
    room_uuid: UUID | None = None,
    agent_uuid: UUID | None = None,
    statuses: tuple[str, ...] = ("active", "candidate"),
) -> "MemoryClaim | None":
    """Return an existing *live* claim (active/candidate by default) in the same
    scope/room whose text normalizes equal to `text`, or None. Used to avoid
    storing the same belief twice — the exact-normalized-duplicate rule from
    docs/memory-architecture.md §3. A rejected/expired claim does not match, so
    re-remembering something previously forgotten still creates a fresh claim."""
    norm = normalize_claim_text(text)
    if not norm:
        return None
    q = db.session.query(MemoryClaim).filter(
        MemoryClaim.scope == scope, MemoryClaim.status.in_(statuses))
    if room_uuid is not None:
        q = q.filter(MemoryClaim.room_uuid == room_uuid)
    if agent_uuid is not None:
        q = q.filter(MemoryClaim.agent_uuid == agent_uuid)
    for claim in q.order_by(MemoryClaim.id.asc()).all():
        if normalize_claim_text(claim.text) == norm:
            return claim
    return None


def supersede_memory(
    old_uuid: UUID,
    new_claim_args: dict[str, Any],
    evidence_args: dict[str, Any],
    commit: bool = True,
) -> MemoryClaim:
    """In one transaction: mark `old_uuid` as superseded, create a new
    `active` claim with `new_claim_args` (its supersedes_uuid wired to
    `old_uuid`), and attach `evidence_args` to the new claim."""
    old = db.session.query(MemoryClaim).filter_by(uuid=old_uuid).first()
    if old is None:
        raise ValueError(f"memory claim not found: {old_uuid}")
    old.status = "superseded"
    new_args = dict(new_claim_args)
    new_args["supersedes_uuid"] = old_uuid
    new_args.setdefault("status", "active")
    new_claim = MemoryClaim(**new_args)
    db.session.add(new_claim)
    db.session.flush()  # assigns new_claim.uuid
    ev = MemoryEvidence(memory_uuid=new_claim.uuid, **evidence_args)
    db.session.add(ev)
    db.session.flush()
    if commit:
        db.session.commit()
    return new_claim


def activate_memory_claim(
    memory_uuid: UUID, *, confirmed_by_uuid: UUID | None = None
) -> MemoryClaim:
    """Promote a claim to `active` and record a confirmation evidence row. Used
    by the confirm-tier activate_memory write once an operator approves it."""
    claim = db.session.query(MemoryClaim).filter_by(uuid=memory_uuid).first()
    if claim is None:
        raise ValueError(f"memory claim not found: {memory_uuid}")
    claim.status = "active"
    claim.updated_at = datetime.now(UTC)
    db.session.add(
        MemoryEvidence(
            memory_uuid=memory_uuid, provenance="confirmed_by_user",
            source_type="manual", created_by_uuid=confirmed_by_uuid,
        )
    )
    db.session.commit()
    return claim


def reject_memory(memory_uuid: UUID, evidence_args: dict[str, Any], commit: bool = True) -> None:
    """Mark a claim `rejected`, attach an evidence row recording the rejection,
    and write a tombstone so the value is not re-learned. Existing evidence is
    left untouched (the audit trail survives)."""
    claim = db.session.query(MemoryClaim).filter_by(uuid=memory_uuid).first()
    if claim is None:
        raise ValueError(f"memory claim not found: {memory_uuid}")
    claim.status = "rejected"
    ev = MemoryEvidence(memory_uuid=memory_uuid, **evidence_args)
    db.session.add(ev)
    db.session.flush()
    write_tombstone(claim, reason="rejected by operator", commit=False)
    if commit:
        db.session.commit()


# --- memory review UI: edits, reactivate, detail, guards ----------------------

_SENSITIVITIES = ("public", "private", "secret")


class StaleWriteError(Exception):
    """A guarded write was refused because the claim changed since the caller
    last read it (its `updated_at` no longer matches `expected_updated_at`).
    The mirror, at single-row granularity, of the `/cron` tree version guard —
    the web layer maps it to HTTP 409."""


def assert_claim_unchanged(claim: MemoryClaim, expected_updated_at: datetime | None) -> None:
    """Optimistic-concurrency check: raise StaleWriteError if the claim's
    `updated_at` differs from what the caller saw. `None` skips the check (an
    unconditional write)."""
    if expected_updated_at is not None and claim.updated_at != expected_updated_at:
        raise StaleWriteError(
            f"memory claim {claim.uuid} changed since it was read "
            f"(expected {expected_updated_at}, found {claim.updated_at})"
        )


def claim_stale(claim: MemoryClaim) -> bool:
    """True when an `active` claim has expired by wall clock (its `expires_at`
    is in the past). Retrieval already excludes these; the UI badges them. A
    non-active claim is never `stale` — its status already explains it."""
    return (
        claim.status == "active"
        and claim.expires_at is not None
        and claim.expires_at <= datetime.now(UTC)
    )


def set_memory_sensitivity(
    memory_uuid: UUID, sensitivity: str, *, expected_updated_at: datetime | None = None
) -> MemoryClaim:
    """Change a claim's sensitivity (policy metadata, not a belief — so no
    evidence row). Guarded by `expected_updated_at`."""
    if sensitivity not in _SENSITIVITIES:
        raise ValueError(f"invalid sensitivity: {sensitivity!r}")
    claim = db.session.query(MemoryClaim).filter_by(uuid=memory_uuid).first()
    if claim is None:
        raise ValueError(f"memory claim not found: {memory_uuid}")
    assert_claim_unchanged(claim, expected_updated_at)
    claim.sensitivity = sensitivity
    claim.updated_at = datetime.now(UTC)
    db.session.commit()
    return claim


def set_memory_expiry(
    memory_uuid: UUID,
    expires_at: datetime | None,
    *,
    expected_updated_at: datetime | None = None,
) -> MemoryClaim:
    """Set or clear a claim's `expires_at` (pass None to clear). Guarded by
    `expected_updated_at`."""
    claim = db.session.query(MemoryClaim).filter_by(uuid=memory_uuid).first()
    if claim is None:
        raise ValueError(f"memory claim not found: {memory_uuid}")
    assert_claim_unchanged(claim, expected_updated_at)
    claim.expires_at = expires_at
    claim.updated_at = datetime.now(UTC)
    db.session.commit()
    return claim


def reactivate_memory_claim(
    memory_uuid: UUID,
    *,
    confirmed_by_uuid: UUID | None = None,
    expected_updated_at: datetime | None = None,
) -> MemoryClaim:
    """Bring a `rejected` or `expired` claim back to `active`, recording a
    confirmation evidence row. Refuses any other starting status (an active or
    superseded claim is not a thing to "reactivate"). Guarded by
    `expected_updated_at`. The DB-level sibling of the assistant's internal
    forget-undo inverse."""
    claim = db.session.query(MemoryClaim).filter_by(uuid=memory_uuid).first()
    if claim is None:
        raise ValueError(f"memory claim not found: {memory_uuid}")
    if claim.status not in ("rejected", "expired"):
        raise ValueError(
            f"cannot reactivate a {claim.status} claim (only rejected/expired)")
    assert_claim_unchanged(claim, expected_updated_at)
    claim.status = "active"
    claim.updated_at = datetime.now(UTC)
    db.session.add(
        MemoryEvidence(
            memory_uuid=memory_uuid, provenance="confirmed_by_user",
            source_type="manual", created_by_uuid=confirmed_by_uuid,
        )
    )
    db.session.commit()
    return claim


def memory_claim_detail(memory_uuid: UUID) -> dict[str, Any] | None:
    """Assemble a claim with everything the detail pane shows: its evidence
    (newest first) and its supersession lineage (the claim it supersedes, and
    the claim that superseded it, if any). Returns None when the claim is
    absent. Embedding/retrieval state is computed in the web layer, which may
    import the embedding module; this stays pure DB."""
    claim = db.session.query(MemoryClaim).filter_by(uuid=memory_uuid).first()
    if claim is None:
        return None
    evidence = (
        db.session.query(MemoryEvidence)
        .filter_by(memory_uuid=memory_uuid)
        .order_by(MemoryEvidence.id.desc())
        .all()
    )
    supersedes = None
    if claim.supersedes_uuid is not None:
        supersedes = (
            db.session.query(MemoryClaim)
            .filter_by(uuid=claim.supersedes_uuid)
            .first()
        )
    superseded_by = (
        db.session.query(MemoryClaim)
        .filter_by(supersedes_uuid=memory_uuid)
        .first()
    )
    return {
        "claim": claim,
        "evidence": evidence,
        "supersedes": supersedes,
        "superseded_by": superseded_by,
    }


# --- embeddings (hybrid retrieval) -------------------------------------------


def upsert_memory_embedding(
    *,
    memory_uuid: UUID,
    model_name: str,
    embed_dim: int,
    text_hash: str,
    embedding: list[float],
) -> MemoryEmbedding:
    """Insert or update the embedding row for (memory_uuid, model_name,
    text_hash). Idempotent on the unique key."""
    row = (
        db.session.query(MemoryEmbedding)
        .filter(
            MemoryEmbedding.memory_uuid == memory_uuid,
            MemoryEmbedding.model_name == model_name,
            MemoryEmbedding.text_hash == text_hash,
        )
        .one_or_none()
    )
    if row is None:
        row = MemoryEmbedding(
            memory_uuid=memory_uuid, model_name=model_name,
            embed_dim=embed_dim, text_hash=text_hash, embedding=embedding,
        )
        db.session.add(row)
    else:
        row.embedding = embedding
        row.embed_dim = embed_dim
        row.updated_at = datetime.now(UTC)
    db.session.commit()
    return row


def get_memory_embedding(
    memory_uuid: UUID, model_name: str
) -> MemoryEmbedding | None:
    """The most recent embedding row for a claim under one model, or None."""
    return (
        db.session.query(MemoryEmbedding)
        .filter(
            MemoryEmbedding.memory_uuid == memory_uuid,
            MemoryEmbedding.model_name == model_name,
        )
        .order_by(MemoryEmbedding.id.desc())
        .first()
    )


def delete_memory_embeddings(memory_uuid: UUID) -> int:
    """Drop all embedding rows for a claim. Returns the number removed."""
    n = (
        db.session.query(MemoryEmbedding)
        .filter(MemoryEmbedding.memory_uuid == memory_uuid)
        .delete()
    )
    db.session.commit()
    return n


# --- tombstone (anti-laundering) helpers -------------------------------------

_NIL_UUID = "00000000-0000-0000-0000-000000000000"


def with_note(evidence: dict[str, Any], note: str) -> dict[str, Any]:
    """Return a copy of `evidence` with `note` appended to `excerpt` (joined with
    "; ") — never passes a duplicate excerpt kwarg into add_memory_evidence."""
    out = dict(evidence)
    existing = out.get("excerpt")
    out["excerpt"] = f"{existing}; {note}" if existing else note
    return out


def evidence_summary(evidence: dict[str, Any]) -> str:
    """Compact provenance digest stored on a tombstone snapshot."""
    return "/".join(str(evidence.get(k, "")) for k in
                    ("provenance", "source_type", "source_id"))


def advisory_key(scope, room_uuid, agent_uuid, sp_key, value_key) -> int:
    """Stable signed 63-bit int for pg_advisory_xact_lock, derived from the
    belief-key tuple."""
    raw = "|".join((scope, str(room_uuid or _NIL_UUID), str(agent_uuid or _NIL_UUID),
                    sp_key, value_key))
    h = int.from_bytes(_hashlib.blake2b(raw.encode(), digest_size=8).digest(), "big")
    return h - (1 << 63)   # map to signed range


def write_tombstone(claim, *, reason, created_by_uuid=None, commit: bool = True):
    """Upsert a tombstone for `claim`'s (scope, key, value), snapshotting its text
    and a one-line evidence digest. Idempotent on the unique key.

    Global-scope tombstones are keyed on (scope="global", room_uuid=None,
    agent_uuid=None) regardless of the claim's own room_uuid — a global rejection
    is cross-room."""
    sp, val = belief_keys(claim.subject, claim.predicate, claim.object, claim.text)
    # Normalize: global tombstones are not room- or agent-scoped.
    tomb_room = None if claim.scope == "global" else claim.room_uuid
    tomb_agent = None if claim.scope == "global" else claim.agent_uuid
    existing = check_tombstone(claim.scope, tomb_room, tomb_agent, sp, val)
    latest_ev = (db.session.query(MemoryEvidence)
                 .filter_by(memory_uuid=claim.uuid)
                 .order_by(MemoryEvidence.id.desc()).first())
    ev_sum = evidence_summary({
        "provenance": getattr(latest_ev, "provenance", ""),
        "source_type": getattr(latest_ev, "source_type", ""),
        "source_id": getattr(latest_ev, "source_id", ""),
    }) if latest_ev else None
    if existing is not None:
        existing.reason = reason
        existing.claim_text = claim.text
        existing.evidence_summary = ev_sum
        existing.created_from_uuid = claim.uuid
        row = existing
    else:
        row = MemoryRejectedValue(
            scope=claim.scope, room_uuid=tomb_room, agent_uuid=tomb_agent,
            subj_pred_key=sp, value_key=val, claim_text=claim.text,
            evidence_summary=ev_sum, reason=reason, created_from_uuid=claim.uuid,
            created_by_uuid=created_by_uuid, hit_count=0)
        db.session.add(row)
    db.session.flush()
    if commit:
        db.session.commit()
    return row


def check_tombstone(scope, room_uuid, agent_uuid, sp_key, value_key):
    """Exact-scope tombstone lookup. Callers consult exact + global separately."""
    q = db.session.query(MemoryRejectedValue).filter(
        MemoryRejectedValue.scope == scope,
        MemoryRejectedValue.subj_pred_key == sp_key,
        MemoryRejectedValue.value_key == value_key)
    q = (q.filter(MemoryRejectedValue.room_uuid == room_uuid) if room_uuid is not None
         else q.filter(MemoryRejectedValue.room_uuid.is_(None)))
    q = (q.filter(MemoryRejectedValue.agent_uuid == agent_uuid) if agent_uuid is not None
         else q.filter(MemoryRejectedValue.agent_uuid.is_(None)))
    return q.first()


def clear_tombstone(tomb, *, commit: bool = True) -> None:
    db.session.delete(tomb)
    db.session.flush()
    if commit:
        db.session.commit()


def record_tombstone_hit(tomb, *, commit: bool = True) -> None:
    tomb.hit_count = (tomb.hit_count or 0) + 1
    tomb.last_hit_at = datetime.now(UTC)
    db.session.flush()
    if commit:
        db.session.commit()


def list_tombstones_with_hits(*, room_uuid: UUID | None = None
                              ) -> list[MemoryRejectedValue]:
    q = db.session.query(MemoryRejectedValue).filter(MemoryRejectedValue.hit_count > 0)
    if room_uuid is not None:
        q = q.filter(MemoryRejectedValue.room_uuid == room_uuid)
    return q.order_by(MemoryRejectedValue.last_hit_at.desc()).all()


# --- record_belief: the single governed write path ---------------------------

TOMBSTONE_OVERRIDE_ACTORS = {
    "human_review_ui", "explicit_human_command", "human_confirmed_write_intent",
}

# per-source_type evidence requirements: field -> required?
_EVIDENCE_MATRIX = {
    "chat_message": {"source_id": True, "excerpt": True, "created_by_uuid": True},
    "journal":      {"source_id": True, "excerpt": True, "created_by_uuid": True},
    "transcript":   {"source_id": True, "excerpt": True, "created_by_uuid": False},
    "file":         {"source_id": True, "excerpt": True, "created_by_uuid": False},
    "api":          {"source_id": True, "excerpt": False, "created_by_uuid": False},
    "manual":       {"source_id": False, "excerpt": True, "created_by_uuid": False},
}


def validate_evidence(evidence: dict[str, Any]) -> None:
    """Enforce the per-source_type evidence matrix (spec §3.4). Raises ValueError
    on a missing required field. provenance + source_type always required."""
    if not evidence.get("provenance"):
        raise ValueError("evidence.provenance is required")
    st = evidence.get("source_type")
    if st not in _EVIDENCE_MATRIX:
        raise ValueError(f"evidence.source_type invalid: {st!r}")
    for field, required in _EVIDENCE_MATRIX[st].items():
        if required and not evidence.get(field):
            raise ValueError(f"evidence.{field} required for source_type={st!r}")


@dataclass
class BeliefWriteResult:
    outcome: str
    claim: "MemoryClaim | None"
    reason: str | None = None
    conflicts_with_uuid: "UUID | None" = None


def _lock_belief(scope, room_uuid, agent_uuid, sp_key, val_key) -> None:
    """Take advisory locks covering the exact-scope key and the global key, in
    sorted order to avoid deadlock."""
    keys = sorted({
        advisory_key(scope, room_uuid, agent_uuid, sp_key, val_key),
        advisory_key("global", None, None, sp_key, val_key),
    })
    for k in keys:
        db.session.execute(sa.text("SELECT pg_advisory_xact_lock(:k)"), {"k": k})


def record_belief(*, actor, scope, kind, text, confidence, evidence,
                  sensitivity="private", agent_uuid=None, room_uuid=None,
                  subject=None, predicate=None, object=None, expires_at=None
                  ) -> BeliefWriteResult:
    """The single governed write path (spec §3). One atomic transaction:
    dedupe -> tombstone (exact+global) -> [conflict, Task 7] -> create. Never
    raises for policy outcomes; raises ValueError for incomplete evidence."""
    validate_evidence(evidence)
    sp_key, val_key = belief_keys(subject, predicate, object, text)
    _lock_belief(scope, room_uuid, agent_uuid, sp_key, val_key)
    human = actor in TOMBSTONE_OVERRIDE_ACTORS

    # 1. Dedupe
    existing = find_equivalent_claim(text, scope=scope, room_uuid=room_uuid,
                                     agent_uuid=agent_uuid,
                                     statuses=("active", "candidate"))
    if existing is not None:
        existing.support_count = (existing.support_count or 1) + 1
        existing.epistemic_confidence = min(
            1.0, (existing.epistemic_confidence or existing.confidence) + 0.05)
        add_memory_evidence(memory_uuid=existing.uuid, commit=False, **evidence)
        db.session.commit()
        return BeliefWriteResult("corroborated", existing)

    # 2. Tombstone — exact + global, considered separately (spec §3.3/§5)
    cleared_exact = False
    exact = check_tombstone(scope, room_uuid, agent_uuid, sp_key, val_key)
    glob = (check_tombstone("global", None, None, sp_key, val_key)
            if scope != "global" else None)
    if exact is not None and human:
        clear_tombstone(exact, commit=False)
        exact = None
        cleared_exact = True
    if glob is not None:
        if human:
            ev = with_note(evidence, "scoped exception over global tombstone")
            new = create_memory_claim(
                scope=scope, kind=kind, text=text, confidence=confidence,
                status="active", sensitivity=sensitivity, agent_uuid=agent_uuid,
                room_uuid=room_uuid, subject=subject, predicate=predicate, object=object,
                support_count=1, epistemic_confidence=confidence,
                retrieval_strength=confidence, subj_pred_key=sp_key, value_key=val_key,
                key_version=KEY_VERSION, expires_at=expires_at, commit=False)
            add_memory_evidence(memory_uuid=new.uuid, commit=False, **ev)
            db.session.commit()
            return BeliefWriteResult("created", new,
                                     reason="scoped exception; global tombstone intact")
        record_tombstone_hit(glob, commit=False)
        db.session.commit()
        return BeliefWriteResult("refused_tombstone", None,
                                 reason="value previously rejected (global)")
    if exact is not None:   # non-override actor, exact tombstone
        record_tombstone_hit(exact, commit=False)
        db.session.commit()
        return BeliefWriteResult("refused_tombstone", None,
                                 reason="value previously rejected")

    # 3. (conflict detection added in Task 7)

    # 4. Plain create
    status = "active" if human else "candidate"
    ev = with_note(evidence, "operator override of prior rejection") if cleared_exact else evidence
    new = create_memory_claim(
        scope=scope, kind=kind, text=text, confidence=confidence, status=status,
        sensitivity=sensitivity, agent_uuid=agent_uuid, room_uuid=room_uuid,
        subject=subject, predicate=predicate, object=object, support_count=1,
        epistemic_confidence=confidence, retrieval_strength=confidence,
        subj_pred_key=sp_key, value_key=val_key, key_version=KEY_VERSION,
        expires_at=expires_at, commit=False)
    add_memory_evidence(memory_uuid=new.uuid, commit=False, **ev)
    db.session.commit()
    return BeliefWriteResult("created", new)
