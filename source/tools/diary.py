"""Diary operator CLI: `python -m tools.diary <command> --database-url URL ...`.

Run from `source/`. Every command requires `--database-url`; it is parsed and
checked, and DATABASE_URL set, *before* `db` is imported, so no default
database is ever touched by accident. A database other than the sandbox
(`rainbox_claude` or `rainbox_diary_test_*`) additionally needs `--production`;
pilot commands refuse it outright. Nothing here imports webapp, starts agents
or fires cron.

Reports are JSON on stdout. They carry counts, codes, offsets and hashes;
diary text is printed only by `show`, the operator's own inspection command.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

SANDBOX_DATABASES = ("rainbox_claude",)
SANDBOX_PREFIX = "rainbox_diary_test_"
PILOT_COMMANDS = {"reset-pilot"}


class CliError(SystemExit):
    def __init__(self, message: str):
        super().__init__(f"diary: {message}")


def database_name(url: str) -> str:
    from sqlalchemy.engine import make_url

    try:
        name = make_url(url).database
    except Exception as exc:  # noqa: BLE001 — any parse failure is a refusal
        raise CliError(f"cannot parse --database-url: {exc}") from exc
    if not name:
        raise CliError("--database-url names no database")
    return name


def is_sandbox(name: str) -> bool:
    return name in SANDBOX_DATABASES or name.startswith(SANDBOX_PREFIX)


def check_database(url: str, *, production: bool, pilot: bool) -> str:
    """Refuse before any connection: pilot work only on a sandbox database,
    anything else on a non-sandbox database only with --production."""
    name = database_name(url)
    sandbox = is_sandbox(name)
    if pilot and not sandbox:
        raise CliError(f"pilot commands run only on a sandbox database, not {name!r}")
    if not sandbox and not production:
        raise CliError(f"{name!r} is not a sandbox database; pass --production to use it")
    return name


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m tools.diary", description=__doc__.split("\n")[0])
    p.add_argument("--database-url", required=True)
    p.add_argument("--production", action="store_true",
                   help="allow a database other than the sandbox")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("register", help="validate a manifest and create/update a disabled source")
    r.add_argument("--manifest", required=True)
    r.add_argument("--pilot", action="store_true", help="mark a sandbox pilot source")

    sub.add_parser("list", help="list sources")

    for name, helptext in (
        ("parse", "inspect how every file parses; no DB mutation, no model calls"),
        ("sync", "publish snapshots, parses, identifiers and FTS"),
        ("enable", "make the source visible to the assistant"),
        ("disable", "hide the source immediately"),
        ("purge", "disable and remove imported content; keep config/exclusions"),
        ("reset-pilot", "sandbox-only purge of a pilot source"),
    ):
        c = sub.add_parser(name, help=helptext)
        c.add_argument("--source", required=True, help="source UUID or name")
        if name == "parse":
            c.add_argument("--dry-run", action="store_true", required=True)

    for name in ("exclude", "unexclude", "prune"):
        c = sub.add_parser(name)
        c.add_argument("--source", required=True)
        c.add_argument("--path", required=True, help="file path relative to the source root")

    rc = sub.add_parser("reconcile", help="exclude or release files held after an excluded file vanished")
    rc.add_argument("--source", required=True)
    rc.add_argument("--exclude", action="append", default=[])
    rc.add_argument("--release", action="append", default=[])

    s = sub.add_parser("show", help="print a citation's original bytes (operator only)")
    s.add_argument("--citation", required=True)

    e = sub.add_parser("embed", help="resume missing vectors for current passages")
    e.add_argument("--source", required=True)
    e.add_argument("--base-url", default=None, help="loopback embedding endpoint (default OLLAMA_BASE_URL)")
    e.add_argument("--model", default="embeddinggemma:300m")
    e.add_argument("--input-format", type=int, choices=(1, 2), default=1)

    ix = sub.add_parser("index", help="build optional indexes; never enables a mode")
    ix.add_argument("--source", required=True)
    ix.add_argument("--hnsw", action="store_true")
    ix.add_argument("--trgm", action="store_true")

    vm = sub.add_parser("set-vector-mode")
    vm.add_argument("--source", required=True)
    vm.add_argument("--mode", required=True, choices=("off", "exact", "hnsw"))

    pr = sub.add_parser("probe", help="run fixed private queries through the trusted context")
    pr.add_argument("--source", required=True)
    pr.add_argument("--cases", required=True)
    pr.add_argument("--vectors", choices=("off", "exact", "hnsw"), default=None)
    pr.add_argument("--hnsw-gate", action="store_true",
                    help="measure HNSW against exact and record a pass")
    return p


def _emit(obj: Any) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True, default=str))


def run(args: argparse.Namespace) -> Any:
    import db
    from diary.config import load_manifest, ManifestError

    cmd = args.command
    if cmd == "register":
        with open(args.manifest, encoding="utf-8") as fh:
            raw = json.load(fh)
        try:
            manifest = load_manifest(raw)
        except ManifestError as exc:
            raise CliError(f"manifest: {exc}") from exc
        if args.pilot and not is_sandbox(database_name(args.database_url)):
            raise CliError("--pilot only on a sandbox database")
        source, created = db.diary_register_source(manifest, pilot=args.pilot)
        return {"source_uuid": source.uuid, "created": created, "enabled": source.enabled,
                "parser_fingerprint": source.parser_fingerprint,
                "policy_version": source.policy_version}
    if cmd == "list":
        return [{"source_uuid": s.uuid, "name": s.name, "enabled": s.enabled,
                 "sensitivity": s.sensitivity, "pilot": s.pilot, "vector_mode": s.vector_mode,
                 "policy_version": s.policy_version, "catalog_version": s.catalog_version,
                 "files": db.session.query(db.DiaryFile).filter_by(source_uuid=s.uuid).count()}
                for s in db.diary_list_sources()]
    if cmd == "show":
        return _show(args.citation)

    source = db.diary_get_source(args.source)
    if cmd == "parse":
        return _dry_run(source)
    if cmd == "sync":
        from diary.ingest import sync_source
        return sync_source(source.uuid).to_json()
    if cmd in ("enable", "disable"):
        s = db.diary_set_enabled(source.uuid, cmd == "enable")
        return {"source_uuid": s.uuid, "enabled": s.enabled, "policy_version": s.policy_version}
    if cmd == "exclude":
        return {"excluded": db.diary_exclude(source.uuid, args.path)}
    if cmd == "unexclude":
        return {"unexcluded": db.diary_unexclude(source.uuid, args.path)}
    if cmd == "prune":
        return {"revisions_deleted": db.diary_prune(source.uuid, args.path)}
    if cmd == "reconcile":
        return {"still_held": db.diary_reconcile(source.uuid, exclude=args.exclude,
                                                  release=args.release)}
    if cmd == "purge":
        return {"files_removed": db.diary_purge(source.uuid)}
    if cmd == "embed":
        from diary.embeddings import OllamaEmbedder, capture_spec, default_base_url, embed_source
        base = (args.base_url or default_base_url()).rstrip("/")
        embedder = OllamaEmbedder(base, args.model)
        spec = capture_spec(embedder, base_url=base, model=args.model, input_format=args.input_format)
        return embed_source(source.uuid, embedder, spec).__dict__
    if cmd == "index":
        from diary.embeddings import build_hnsw_index, build_trgm_index
        out = {}
        if args.hnsw:
            out["hnsw"] = build_hnsw_index()
        if args.trgm:
            out["trgm"] = build_trgm_index()
        if not out:
            raise CliError("pass --hnsw and/or --trgm")
        return out
    if cmd == "set-vector-mode":
        from diary.embeddings import set_vector_mode
        s = set_vector_mode(source.uuid, args.mode)
        return {"source_uuid": s.uuid, "vector_mode": s.vector_mode}
    if cmd == "probe":
        from diary.embeddings import QueryEmbedder
        from diary.probe import hnsw_gate, run_probe
        with open(args.cases, encoding="utf-8") as fh:
            cases = json.load(fh)["cases"]
        embed_query = QueryEmbedder() if source.embedding_spec else None
        if args.hnsw_gate:
            if embed_query is None:
                raise CliError("no recorded embedding epoch; run embed first")
            return hnsw_gate(source, cases, embed_query)
        return run_probe(source, cases, vectors=args.vectors, embed_query=embed_query)
    if cmd == "reset-pilot":
        if not source.pilot:
            raise CliError(f"source {source.name!r} has no pilot marker; refusing")
        return {"files_removed": db.diary_purge(source.uuid)}
    raise CliError(f"unknown command {cmd!r}")


def _dry_run(source: Any) -> dict[str, Any]:
    """Parse every included file with the source's configuration. Reports
    per-file dialect, counts and diagnostics (codes and offsets only)."""
    import db
    from diary.ingest import enumerate_files, stable_read
    from diary.parsing import parse_file

    exclusions = db.diary_exclusions(source.uuid)
    out: dict[str, Any] = {"source_uuid": source.uuid, "files": []}
    totals = {"entries": 0, "passages": 0, "diagnostics": 0, "quarantined": 0}
    for path in enumerate_files(source.root_path, source.config.get("include_suffixes", [".txt"])):
        if path in exclusions:
            out["files"].append({"path": path, "excluded": True})
            continue
        raw = stable_read(os.path.join(source.root_path, path))
        if raw is None:
            out["files"].append({"path": path, "unstable_read": True})
            continue
        parsed = parse_file(raw, path, source.parser_config)
        n_passages = sum(len(e.passages) for e in parsed.entries)
        totals["entries"] += len(parsed.entries)
        totals["passages"] += n_passages
        totals["diagnostics"] += len(parsed.diagnostics)
        totals["quarantined"] += parsed.status != "ok"
        out["files"].append({
            "path": path, "status": parsed.status, "dialect": parsed.dialect,
            "bytes": len(raw), "entries": len(parsed.entries), "passages": n_passages,
            "undated": sum(e.date_local is None for e in parsed.entries),
            "diagnostics": parsed.diagnostics_json(),
        })
    out["totals"] = totals
    return out


def _show(locator: str) -> dict[str, Any]:
    import db
    from diary.citations import parse_citation

    c = parse_citation(locator)
    if c is None:
        raise CliError("not a citation locator (diary:<revision>:<start>-<end>)")
    rev = db.session.get(db.DiaryRevision, c.revision_uuid)
    if rev is None or c.byte_end > rev.byte_length:
        raise CliError("citation not found")
    f = db.session.get(db.DiaryFile, rev.file_uuid)
    raw = db.diary_revision_bytes(rev)
    gen = db.session.get(db.DiaryGeneration, f.current_generation_uuid) \
        if f.current_generation_uuid else None
    current = gen is not None and gen.revision_uuid == rev.uuid
    entry = None
    if gen is not None:
        entry = (db.session.query(db.DiaryEntry)
                 .filter(db.DiaryEntry.generation_uuid == gen.uuid,
                         db.DiaryEntry.byte_start <= c.byte_start,
                         db.DiaryEntry.byte_end > c.byte_start).one_or_none())
    return {
        "source_uuid": f.source_uuid, "path": f.relative_path,
        "revision_uuid": rev.uuid, "snapshot_ingested_at": rev.first_ingested_at,
        "current_revision": current, "availability": f.availability,
        "date_local": entry.date_local if entry else None,
        "date_basis": entry.date_basis if entry else None,
        "text": raw[c.byte_start:c.byte_end].decode("utf-8", errors="replace"),
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    check_database(args.database_url, production=args.production,
                   pilot=args.command in PILOT_COMMANDS)
    os.environ["DATABASE_URL"] = args.database_url
    import db   # only now: DATABASE_URL is set and checked

    app = db.make_app()
    db.init_db(app)
    with app.app_context():
        _emit(run(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
