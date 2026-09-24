"""Diary persistence: sources, publication, exclusions, retention.

Transactions for the diary tables (db/models.py, "diary memory"). The
filesystem walk and parsing live in `diary.ingest`; this module only writes
rows it is handed. Contract: notes/proposals/2026-09-21-diary-memory-
representation-proposals.md §4 and §6.

Every policy-affecting change bumps `policy_version` (and `catalog_version`,
since visible content changes with it); every publication bumps
`catalog_version`. Embedding writes bump neither.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa

from db.models import (
    Chatroom,
    DiaryAnnotation,
    DiaryEmbedding,
    DiaryEntry,
    DiaryExclusion,
    DiaryFile,
    DiaryGeneration,
    DiaryPassage,
    DiaryRevision,
    DiarySource,
    db,
)

__all__ = [
    "DiaryError",
    "DiaryPolicyChanged",
    "diary_register_source",
    "diary_get_source",
    "diary_list_sources",
    "diary_set_enabled",
    "diary_exclude",
    "diary_unexclude",
    "diary_exclusions",
    "diary_purge",
    "diary_prune",
    "diary_revision_bytes",
    "diary_get_or_create_file",
    "diary_publish",
    "diary_mark_file",
    "diary_reconcile",
    "diary_advisory_key",
]


class DiaryError(ValueError):
    """An operator-facing refusal (unknown source, root change, ...)."""


class DiaryPolicyChanged(RuntimeError):
    """The source's policy changed after work captured it; the work is stale."""


def _now() -> datetime:
    return datetime.now(UTC)


def diary_advisory_key(source_uuid: UUID) -> int:
    """A stable signed 64-bit key for the per-source sync advisory lock."""
    return int.from_bytes(source_uuid.bytes[:8], "big", signed=True)


# --- sources --------------------------------------------------------------------


def diary_get_source(ref: UUID | str) -> DiarySource:
    """By UUID or by unique name. Raises DiaryError when absent."""
    source = None
    try:
        source = db.session.get(DiarySource, UUID(str(ref)))
    except ValueError:
        pass
    if source is None:
        source = db.session.query(DiarySource).filter_by(name=str(ref)).one_or_none()
    if source is None:
        raise DiaryError(f"no diary source {ref!r}")
    return source


def diary_list_sources() -> list[DiarySource]:
    return db.session.query(DiarySource).order_by(DiarySource.name).all()


def _bump(source: DiarySource, *, policy: bool, catalog: bool = True) -> None:
    if policy:
        source.policy_version = (source.policy_version or 0) + 1
    if catalog:
        source.catalog_version = (source.catalog_version or 0) + 1
    source.updated_at = _now()


def diary_register_source(manifest: Any, *, pilot: bool = False) -> tuple[DiarySource, bool]:
    """Create or update the source named by `manifest` (a validated
    diary.config.Manifest). The source UUID and its exclusions are kept; the
    source is (re)disabled and its policy version bumped. A root change is
    refused: citations must never be rebound to another tree. Returns
    (source, created)."""
    from diary.config import build_parser_config, config_fingerprint, manifest_to_json

    room = db.session.query(Chatroom).filter_by(uuid=manifest.room_uuid).one_or_none()
    if room is None:
        raise DiaryError(f"room {manifest.room_uuid} does not exist")
    parser_config = build_parser_config(manifest)
    fingerprint = config_fingerprint(parser_config)
    config = manifest_to_json(manifest)
    source = db.session.query(DiarySource).filter_by(name=manifest.name).one_or_none()
    created = source is None
    if created:
        clash = db.session.query(DiarySource).filter_by(root_path=manifest.root).one_or_none()
        if clash is not None:
            raise DiaryError(f"root {manifest.root!r} already belongs to source {clash.name!r}")
        for other in db.session.query(DiarySource).all():
            a, b = other.root_path.rstrip("/") + "/", manifest.root.rstrip("/") + "/"
            if a.startswith(b) or b.startswith(a):
                raise DiaryError(f"root overlaps source {other.name!r}")
        source = DiarySource(
            uuid=uuid4(), name=manifest.name, root_path=manifest.root,
            room_uuid=manifest.room_uuid, agent_uuid=manifest.agent_uuid,
            timezone=manifest.timezone, sensitivity=manifest.sensitivity,
            allow_remote_models=manifest.allow_remote_models, config=config,
            parser_config=parser_config, parser_fingerprint=fingerprint,
            enabled=False, pilot=pilot, vector_mode="off",
            policy_version=1, catalog_version=1,
        )
        db.session.add(source)
    else:
        if source.root_path != manifest.root:
            raise DiaryError("a root change needs a new source; citations are bound to the old tree")
        if source.pilot != pilot:
            raise DiaryError("the pilot marker is set at creation and cannot change")
        source.room_uuid = manifest.room_uuid
        source.agent_uuid = manifest.agent_uuid
        source.timezone = manifest.timezone
        source.sensitivity = manifest.sensitivity
        source.allow_remote_models = manifest.allow_remote_models
        source.config = config
        source.parser_config = parser_config
        source.parser_fingerprint = fingerprint
        source.enabled = False
        _bump(source, policy=True)
    db.session.commit()
    return source, created


def diary_set_enabled(source_uuid: UUID, enabled: bool) -> DiarySource:
    source = _locked_source(source_uuid)
    if enabled and source.sensitivity == "secret":
        db.session.rollback()
        raise DiaryError("a secret source can be inspected locally but never enabled")
    if source.enabled != enabled:
        source.enabled = enabled
        _bump(source, policy=True)
    db.session.commit()
    return source


def _locked_source(source_uuid: UUID) -> DiarySource:
    source = (db.session.query(DiarySource).filter_by(uuid=source_uuid)
              .with_for_update().one_or_none())
    if source is None:
        raise DiaryError(f"no diary source {source_uuid}")
    return source


# --- exclusions -----------------------------------------------------------------


def diary_exclusions(source_uuid: UUID) -> set[str]:
    rows = db.session.query(DiaryExclusion.relative_path).filter_by(source_uuid=source_uuid)
    return {r[0] for r in rows}


def diary_exclude(source_uuid: UUID, relative_path: str, *, commit: bool = True) -> bool:
    """Exclude one file (all revisions, all routes). Immediate: the policy
    version bump invalidates cursors and in-flight work. Returns False when
    the path was already excluded."""
    source = _locked_source(source_uuid)
    exists = db.session.query(DiaryExclusion).filter_by(
        source_uuid=source_uuid, relative_path=relative_path).one_or_none()
    if exists is None:
        db.session.add(DiaryExclusion(uuid=uuid4(), source_uuid=source_uuid,
                                      relative_path=relative_path))
        _bump(source, policy=True)
    if commit:
        db.session.commit()
    return exists is None


def diary_unexclude(source_uuid: UUID, relative_path: str) -> bool:
    source = _locked_source(source_uuid)
    n = db.session.query(DiaryExclusion).filter_by(
        source_uuid=source_uuid, relative_path=relative_path).delete()
    if n:
        _bump(source, policy=True)
    db.session.commit()
    return bool(n)


# --- purge and prune --------------------------------------------------------------


def diary_purge(source_uuid: UUID) -> int:
    """Disable the source and delete its imported content (files, revisions,
    parses, vectors, cursors). Source configuration and exclusions stay.
    Returns the number of files removed."""
    source = _locked_source(source_uuid)
    source.enabled = False
    _bump(source, policy=True)
    db.session.execute(sa.update(DiaryFile).where(DiaryFile.source_uuid == source_uuid)
                       .values(current_generation_uuid=None))
    db.session.execute(sa.delete(DiaryEmbedding).where(DiaryEmbedding.source_uuid == source_uuid))
    n = db.session.execute(sa.delete(DiaryFile).where(DiaryFile.source_uuid == source_uuid)).rowcount
    db.session.execute(sa.text(
        "DELETE FROM diary_cursor WHERE jsonb_exists(catalog_manifest, :s)"), {"s": str(source_uuid)})
    db.session.commit()
    return n or 0


def diary_prune(source_uuid: UUID, relative_path: str) -> int:
    """Drop one file's non-current revisions. The current revision gets its
    own bytes and the current rows cite it; citations into the pruned
    revisions become not_found. Returns the number of revisions deleted."""
    source = _locked_source(source_uuid)
    f = db.session.query(DiaryFile).filter_by(
        source_uuid=source_uuid, relative_path=relative_path).one_or_none()
    if f is None:
        raise DiaryError(f"no file {relative_path!r} in source {source.name!r}")
    keep: UUID | None = None
    if f.current_generation_uuid is not None:
        gen = db.session.get(DiaryGeneration, f.current_generation_uuid)
        keep = gen.revision_uuid
        rev = db.session.get(DiaryRevision, keep)
        if rev.raw_bytes is None:
            rev.raw_bytes = diary_revision_bytes(rev)
            rev.bytes_in_revision_uuid = None
        rev.extends_revision_uuid = None
        db.session.flush()
        entry_ids = sa.select(DiaryEntry.uuid).where(DiaryEntry.generation_uuid == gen.uuid)
        db.session.execute(sa.update(DiaryEntry).where(DiaryEntry.generation_uuid == gen.uuid)
                           .values(cite_revision_uuid=keep))
        db.session.execute(sa.update(DiaryPassage).where(DiaryPassage.entry_uuid.in_(entry_ids))
                           .values(cite_revision_uuid=keep))
    others = db.session.query(DiaryRevision).filter(DiaryRevision.file_uuid == f.uuid)
    if keep is not None:
        others = others.filter(DiaryRevision.uuid != keep)
    ids = [r.uuid for r in others]
    if ids:
        # One statement: references among the deleted revisions are checked
        # at its end (NO ACTION), when none of them remain.
        db.session.execute(sa.delete(DiaryRevision).where(DiaryRevision.uuid.in_(ids)))
    _bump(source, policy=False)
    db.session.commit()
    return len(ids)


# --- revisions and publication -------------------------------------------------------


def diary_revision_bytes(revision: DiaryRevision) -> bytes:
    """A revision's bytes: its own, or the first byte_length bytes of the one
    revision that stores them (one hop by invariant)."""
    if revision.raw_bytes is not None:
        return bytes(revision.raw_bytes)
    host = db.session.get(DiaryRevision, revision.bytes_in_revision_uuid)
    if host is None or host.raw_bytes is None:
        raise RuntimeError(f"diary revision {revision.uuid} bytes unresolvable")
    return bytes(host.raw_bytes)[: revision.byte_length]


def diary_get_or_create_file(source_uuid: UUID, relative_path: str) -> tuple[DiaryFile, bool]:
    f = db.session.query(DiaryFile).filter_by(
        source_uuid=source_uuid, relative_path=relative_path).one_or_none()
    if f is not None:
        return f, False
    f = DiaryFile(uuid=uuid4(), source_uuid=source_uuid, relative_path=relative_path,
                  availability="pending", diagnostics=[], last_seen_at=_now())
    db.session.add(f)
    db.session.commit()
    return f, True


def diary_mark_file(file_uuid: UUID, *, availability: str | None = None,
                    diagnostics: list[dict] | None = None, policy_version: int | None = None,
                    seen: bool = False) -> None:
    """Set a file's availability/diagnostics in its own transaction, bumping
    the catalog when visibility changes."""
    f = db.session.get(DiaryFile, file_uuid)
    source = _locked_source(f.source_uuid)
    if policy_version is not None and source.policy_version != policy_version:
        db.session.rollback()
        raise DiaryPolicyChanged(str(source.uuid))
    if availability is not None and availability != f.availability:
        was_ready = f.availability == "ready"
        f.availability = availability
        if was_ready or availability == "ready":
            _bump(source, policy=False)
    if diagnostics is not None:
        f.diagnostics = diagnostics
    if seen:
        f.last_seen_at = _now()
    db.session.commit()


@dataclass
class PublishResult:
    generation_uuid: UUID
    revision_uuid: UUID
    new_revision: bool
    extended: bool


def _citations_from(old_gen: DiaryGeneration) -> tuple[dict, dict]:
    entries = {}
    passages = {}
    for e in db.session.query(DiaryEntry).filter_by(generation_uuid=old_gen.uuid):
        entries[(e.byte_start, e.byte_end, e.text)] = e.cite_revision_uuid
    rows = (db.session.query(DiaryPassage)
            .join(DiaryEntry, DiaryEntry.uuid == DiaryPassage.entry_uuid)
            .filter(DiaryEntry.generation_uuid == old_gen.uuid))
    for p in rows:
        passages[(p.byte_start, p.byte_end, p.text_hash)] = p.cite_revision_uuid
    return entries, passages


def diary_publish(file_uuid: UUID, raw: bytes, sha: str, parsed: Any, *,
                  parser_config: dict, fingerprint: str, policy_version: int,
                  diagnostics: list[dict] | None = None) -> PublishResult:
    """Publish one parsed file in one transaction (§6 step 3): revision (with
    prefix-shared storage), generation and all search rows, stable citation
    revisions, pointer switch, deletion of the superseded generation, and a
    catalog bump. Raises DiaryPolicyChanged if the policy moved since capture;
    nothing is written then."""
    f = db.session.get(DiaryFile, file_uuid)
    source = _locked_source(f.source_uuid)
    if source.policy_version != policy_version:
        db.session.rollback()
        raise DiaryPolicyChanged(str(source.uuid))

    rev = db.session.query(DiaryRevision).filter_by(file_uuid=f.uuid, sha256=sha).one_or_none()
    new_revision = rev is None
    if new_revision:
        rev = DiaryRevision(uuid=uuid4(), file_uuid=f.uuid, sha256=sha, byte_length=len(raw),
                            raw_bytes=raw, first_ingested_at=_now())
        db.session.add(rev)
        db.session.flush()

    old_gen = db.session.get(DiaryGeneration, f.current_generation_uuid) \
        if f.current_generation_uuid else None
    old_rev = db.session.get(DiaryRevision, old_gen.revision_uuid) if old_gen else None
    extended = False
    if old_rev is not None:
        extended = old_rev.uuid == rev.uuid or raw.startswith(diary_revision_bytes(old_rev))
    entry_cites, passage_cites = _citations_from(old_gen) if (old_gen and extended) else ({}, {})

    def cite(key: tuple, table: dict, end: int) -> UUID:
        if extended:
            inherited = table.get(key)
            if inherited is not None:
                return inherited
            if end <= old_rev.byte_length:
                return old_rev.uuid
        return rev.uuid

    # A stale generation of this exact (revision, config) can only exist if
    # it is the current one, which the caller treats as unchanged; clear any
    # leftover so the unique key holds.
    stale = sa.delete(DiaryGeneration).where(
        DiaryGeneration.revision_uuid == rev.uuid,
        DiaryGeneration.parser_fingerprint == fingerprint)
    if old_gen is not None:
        stale = stale.where(DiaryGeneration.uuid != old_gen.uuid)
    db.session.execute(stale)
    gen = DiaryGeneration(
        uuid=uuid4(), file_uuid=f.uuid, revision_uuid=rev.uuid,
        parser_fingerprint=fingerprint, parser_config=parser_config,
        dialect=parsed.dialect, diagnostics=parsed.diagnostics_json(),
        coverage=parsed.coverage_json(), created_at=_now())
    db.session.add(gen)
    db.session.flush()

    entry_rows, passage_rows, annotation_rows = [], [], []
    for e in parsed.entries:
        eid = uuid4()
        entry_rows.append({
            "uuid": eid, "generation_uuid": gen.uuid, "ordinal": e.ordinal,
            "byte_start": e.byte_start, "byte_end": e.byte_end,
            "cite_revision_uuid": cite((e.byte_start, e.byte_end, e.text), entry_cites, e.byte_end),
            "text": e.text, "date_local": e.date_local, "clock_start": e.clock_start,
            "clock_end": e.clock_end, "date_basis": e.date_basis, "time_status": e.time_status,
            "author_token": e.author_token,
            "context_ranges": [list(r) for r in e.context_ranges],
        })
        for p in e.passages:
            passage_rows.append({
                "uuid": uuid4(), "entry_uuid": eid, "part_index": p.part_index,
                "byte_start": p.byte_start, "byte_end": p.byte_end,
                "cite_revision_uuid": cite((p.byte_start, p.byte_end, p.text_hash),
                                           passage_cites, p.byte_end),
                "text": p.text, "text_hash": p.text_hash,
            })
        for a in e.annotations:
            annotation_rows.append({
                "uuid": uuid4(), "entry_uuid": eid, "kind": a.kind, "subtype": a.subtype,
                "value": a.value, "byte_start": a.byte_start, "byte_end": a.byte_end,
                "basis": a.basis,
            })
    if entry_rows:
        db.session.execute(sa.insert(DiaryEntry), entry_rows)
    if passage_rows:
        db.session.execute(sa.insert(DiaryPassage), passage_rows)
    if annotation_rows:
        db.session.execute(sa.insert(DiaryAnnotation), annotation_rows)

    # Prefix-shared storage: the previous current revision (and everything
    # stored inside it) now lives in the new revision's bytes.
    if new_revision and old_rev is not None and old_rev.uuid != rev.uuid and extended:
        rev.extends_revision_uuid = old_rev.uuid
        db.session.flush()
        db.session.execute(sa.text(
            "UPDATE diary_revision SET bytes_in_revision_uuid = :new, raw_bytes = NULL "
            "WHERE file_uuid = :f AND (uuid = :old OR bytes_in_revision_uuid = :old)"),
            {"new": rev.uuid, "old": old_rev.uuid, "f": f.uuid})

    f.current_generation_uuid = gen.uuid
    f.availability = "ready"
    f.diagnostics = diagnostics if diagnostics is not None else parsed.diagnostics_json()
    f.last_seen_at = _now()
    if old_gen is not None:
        db.session.flush()
        db.session.execute(sa.delete(DiaryGeneration).where(DiaryGeneration.uuid == old_gen.uuid))
    _bump(source, policy=False)
    db.session.commit()
    return PublishResult(gen.uuid, rev.uuid, new_revision, extended)


def diary_reconcile(source_uuid: UUID, *, exclude: list[str], release: list[str]) -> list[str]:
    """Resolve files held after an excluded file vanished: exclude them, or
    release them to be published by the next sync. Returns the paths still
    held."""
    for path in exclude:
        diary_exclude(source_uuid, path, commit=False)
    held = []
    for f in db.session.query(DiaryFile).filter_by(source_uuid=source_uuid):
        codes = {d.get("code") for d in (f.diagnostics or [])}
        if "held_for_reconcile" not in codes:
            continue
        if f.relative_path in exclude or f.relative_path in release:
            f.diagnostics = [d for d in f.diagnostics if d.get("code") != "held_for_reconcile"]
        else:
            held.append(f.relative_path)
    db.session.commit()
    return held
