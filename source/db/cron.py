"""Cron persistence and scheduler/firing.

Split out of db.py. Holds the cron folder/job/run tree (load/validate/save),
schedule computation, and job firing/tick (fire_cron_job, cron_tick, ...).
Firing posts to chat (db.chat) and enqueues workspace-shell commands (db.queue).
Re-exported from db for import compatibility.
"""
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import sqlalchemy as sa

from db.models import (
    CRON_ROOM_UUID,
    CRON_SYSTEM_UUID,
    Chatroom,
    CronFolder,
    CronJob,
    CronRun,
    db,
)

# Fixed uuids for the seeded "System" folder and its daily database-backup job,
# so seed_cron_defaults stays idempotent across restarts. Random-looking (not
# near-zero) so their short forms are distinguishable in the UI.
SYSTEM_CRON_FOLDER_UUID = UUID("fc467ea3-5700-42ff-a109-9aa311c1886e")
BACKUP_CRON_JOB_UUID = UUID("ea97c5b9-a4cd-4553-97d6-d60c5a4f0e81")

# Cron action types. 'message' posts to chat, 'command' runs via the
# workspace-shell agent, 'backup' dumps the database in-process (see
# fire_cron_job). Shared by the tree validator and the upsert.
CRON_ACTION_TYPES = ("message", "command", "backup")
from db.queue import enqueue
from db.chat import post_chat_message, post_cron_event


def _cron_last_run_brief(run: "CronRun | None") -> dict[str, Any] | None:
    """The list pages' health cell: just the latest run's outcome + context for
    the hover tooltip (the full history is the Job-details health endpoint)."""
    if run is None:
        return None
    return {
        "status": run.status,
        "trigger": run.trigger,
        "debug": run.debug,
        "error": run.error,
        "fired_at": run.fired_at.isoformat() if run.fired_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


def cron_load_tree() -> dict[str, Any]:
    """Return the whole cron tree using the frontend's field names, each list
    ordered by `position` (then id) so the page renders in saved order. Each
    job also carries read-only `last_run` (its latest cron_run outcome, or
    null) for the list pages' health column."""
    folders = db.session.execute(
        sa.select(CronFolder).order_by(CronFolder.position, CronFolder.id)
    ).scalars().all()
    jobs = db.session.execute(
        sa.select(CronJob).order_by(CronJob.position, CronJob.id)
    ).scalars().all()
    # Latest run per job in one pass (Postgres DISTINCT ON).
    latest_run = {
        r.cron_uuid: r
        for r in db.session.execute(
            sa.select(CronRun)
            .distinct(CronRun.cron_uuid)
            .order_by(CronRun.cron_uuid, CronRun.id.desc())
        ).scalars().all()
    }
    return {
        "folders": [
            {
                "id": str(f.uuid),
                "name": f.name,
                "description": f.description,
                "parentId": str(f.parent_uuid) if f.parent_uuid else None,
                "enabled": f.enabled,
                "created_at": f.created_at.isoformat() if f.created_at else None,
                "updated_at": f.updated_at.isoformat() if f.updated_at else None,
            }
            for f in folders
        ],
        "jobs": [
            {
                "uuid": str(j.uuid),
                "name": j.name,
                "enabled": j.enabled,
                "folderId": str(j.folder_uuid) if j.folder_uuid else None,
                "cron": j.cron_expr,
                "timezone": j.timezone,
                "type": j.action_type,
                "target": j.target,
                "message": j.message,
                "command": j.command,
                "description": j.description,
                "maxRetries": j.max_retries,
                "created_at": j.created_at.isoformat() if j.created_at else None,
                "updated_at": j.updated_at.isoformat() if j.updated_at else None,
                "last_run": _cron_last_run_brief(latest_run.get(j.uuid)),
                # Read-only, for the lists' next-run column (the scheduler owns it).
                "next_run_at": j.next_run_at.isoformat() if j.next_run_at else None,
            }
            for j in jobs
        ],
        # Chatrooms for the message-target picker (value = uuid, label = name).
        "chatrooms": cron_chatrooms(),
        # Optimistic-concurrency token; the page echoes it on PUT (409 if stale).
        "version": cron_tree_version(),
        # Global pause state for the page's Pause/Resume toggle.
        "paused": cron_is_paused(),
    }


def cron_is_paused() -> bool:
    """Whether the global cron.paused setting is on (the tick fires nothing)."""
    from db.settings import get_setting

    return bool(get_setting("cron.paused"))


def cron_chatrooms() -> list[dict[str, Any]]:
    """Chatrooms for the /cron message-target picker, name-sorted. Each is
    {uuid, name}; the job stores the uuid so a room rename can't break it."""
    rooms = db.session.execute(
        sa.select(Chatroom).order_by(Chatroom.name)
    ).scalars().all()
    return [{"uuid": str(r.uuid), "name": r.name} for r in rooms]


class CronTreeError(ValueError):
    """A cron tree payload failed structural validation (bad uuid, dangling
    parent, cycle, unknown action type, malformed cron). Callers (the PUT
    endpoint, future MCP tools) turn this into a 4xx rather than a 500."""


class CronTreeConflict(Exception):
    """The tree changed since the caller hydrated (stale base_version on save).
    Callers map this to HTTP 409 so the client re-hydrates instead of silently
    clobbering — and deleting — the other writer's changes."""


def cron_tree_version() -> str:
    """Opaque version token for the persisted tree, derived from the
    user-managed fields only. Scheduler bookkeeping (next_run_at /
    last_fired_at and the updated_at it bumps) is deliberately excluded, so
    background firing never invalidates an open page — only a real edit by
    another writer does. The /cron page hydrates with this token and sends it
    back on PUT; cron_save_tree refuses a save whose token is stale."""
    folders = db.session.execute(
        sa.select(CronFolder).order_by(CronFolder.uuid)
    ).scalars().all()
    jobs = db.session.execute(
        sa.select(CronJob).order_by(CronJob.uuid)
    ).scalars().all()
    payload = [
        [
            [str(f.uuid), f.name, f.description,
             str(f.parent_uuid) if f.parent_uuid else None,
             bool(f.enabled), f.position]
            for f in folders
        ],
        [
            [str(j.uuid), j.name, bool(j.enabled),
             str(j.folder_uuid) if j.folder_uuid else None,
             j.cron_expr, j.timezone, j.action_type,
             j.target, j.message, j.command, j.description,
             j.max_retries, j.position]
            for j in jobs
        ],
    ]
    blob = json.dumps(payload, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


# A cron *field* is `*`, `*/N`, a number, or a comma/range list of those — the
# characters the UI grammar can ever produce. This is a shape check, not full
# semantic validation; the real parser (croniter) arrives with the scheduler.
_CRON_FIELD_RE = re.compile(r"^[0-9*/,\-]+$")

# Which clock a job's cron is evaluated against. 'localtime' = the host's local
# timezone at fire time (so it follows the machine when it travels); 'UTC' =
# fixed. The scheduler honors this when it computes next_run_at (future).
CRON_TIMEZONES = ("localtime", "UTC")

# Upper bound for a job's max_retries — enough for transient flakiness, low
# enough that a misconfigured job can't hammer a failing command.
CRON_MAX_RETRIES_CAP = 10


def _to_uuid(value: Any) -> UUID | None:
    """Parse to a `UUID` (normalizing case/format) or None if it isn't one.
    Returning the UUID object lets callers key dedup/reference checks on the
    *normalized* value, so case-variant spellings of the same uuid collide here
    instead of slipping through to the DB unique constraint."""
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def validate_cron_expr(expr: Any) -> None:
    """Reject anything that isn't a 5-field cron of UI-shaped tokens."""
    if not isinstance(expr, str):
        raise CronTreeError(f"cron expression must be a string, got {type(expr).__name__}")
    parts = expr.split()
    if len(parts) != 5:
        raise CronTreeError(f"cron expression must have 5 fields: {expr!r}")
    for part in parts:
        if not _CRON_FIELD_RE.match(part):
            raise CronTreeError(f"invalid cron field {part!r} in {expr!r}")


def validate_cron_tree(
    folders: list[dict[str, Any]], jobs: list[dict[str, Any]]
) -> None:
    """Structural integrity check for an incoming cron tree, run before any DB
    write. The /cron frontend always sends well-formed payloads, but the bulk
    PUT (and later MCP/agent editors) shouldn't be able to persist a tree with
    bad uuids, dangling/cyclic folder references, unknown action types, or
    malformed cron — those would corrupt the tree or crash the scheduler.

    Raises CronTreeError on the first problem; does not touch the DB. uuids are
    normalized to `UUID` objects so case/format-variant spellings of the same id
    collide here (a duplicate/dangling ref) instead of reaching the DB."""
    # Payload shape: both must be lists of objects (a malformed body like
    # {"folders": "bad"} must be a 400, not an AttributeError → 500).
    if not isinstance(folders, list):
        raise CronTreeError(f"'folders' must be a list, got {type(folders).__name__}")
    if not isinstance(jobs, list):
        raise CronTreeError(f"'jobs' must be a list, got {type(jobs).__name__}")
    # Folders: unique uuid ids, string names, parent is null or a known folder.
    parent_of: dict[UUID, UUID | None] = {}
    for f in folders:
        if not isinstance(f, dict):
            raise CronTreeError(f"folder entry must be an object, got {type(f).__name__}")
        fid = _to_uuid(f.get("id"))
        if fid is None:
            raise CronTreeError(f"folder id is not a uuid: {f.get('id')!r}")
        if fid in parent_of:
            raise CronTreeError(f"duplicate folder id: {fid}")
        if not isinstance(f.get("name", ""), str):
            raise CronTreeError(f"folder {fid} name must be a string")
        if not isinstance(f.get("description", ""), str):
            raise CronTreeError(f"folder {fid} description must be a string")
        pid_raw = f.get("parentId")
        if pid_raw is None:
            pid: UUID | None = None
        else:
            pid = _to_uuid(pid_raw)
            if pid is None:
                raise CronTreeError(f"folder {fid} parentId is not a uuid: {pid_raw!r}")
        parent_of[fid] = pid
    for fid, pid in parent_of.items():
        if pid is not None and pid not in parent_of:
            raise CronTreeError(f"folder {fid} references missing parent {pid}")
    # Acyclic: walking parents from any folder must terminate at a root.
    for start in parent_of:
        seen: set[UUID] = set()
        cur = parent_of[start]
        while cur is not None:
            if cur == start or cur in seen:
                raise CronTreeError(f"folder cycle detected involving {start}")
            seen.add(cur)
            cur = parent_of.get(cur)
    # Jobs: unique uuids, known action type, folder is null or a known folder,
    # cron well-shaped.
    job_uuids: set[UUID] = set()
    for j in jobs:
        if not isinstance(j, dict):
            raise CronTreeError(f"job entry must be an object, got {type(j).__name__}")
        ju = _to_uuid(j.get("uuid"))
        if ju is None:
            raise CronTreeError(f"job uuid is not a uuid: {j.get('uuid')!r}")
        if ju in job_uuids:
            raise CronTreeError(f"duplicate job uuid: {ju}")
        # uuids must be unique *across kinds* too: a node is identified globally
        # by uuid (e.g. /cron?id=<uuid>), so a job sharing a folder's uuid would
        # make that deep link ambiguous.
        if ju in parent_of:
            raise CronTreeError(f"job uuid {ju} collides with a folder id")
        job_uuids.add(ju)
        atype = j.get("type", "message")
        if atype not in CRON_ACTION_TYPES:
            raise CronTreeError(f"job {ju} has unknown type {atype!r}")
        # A message target is a chatroom uuid (rename-proof). Empty is allowed
        # (firing falls back to the cron room); a non-uuid is rejected so a stale
        # name can't be saved.
        if atype == "message":
            tgt = j.get("target", "")
            if tgt and _to_uuid(tgt) is None:
                raise CronTreeError(
                    f"job {ju} message target must be a chatroom uuid: {tgt!r}"
                )
        fld_raw = j.get("folderId")
        if fld_raw is not None:
            fld = _to_uuid(fld_raw)
            if fld is None:
                raise CronTreeError(f"job {ju} folderId is not a uuid: {fld_raw!r}")
            if fld not in parent_of:
                raise CronTreeError(f"job {ju} references missing folder {fld}")
        tz = j.get("timezone", "localtime")
        if tz not in CRON_TIMEZONES:
            raise CronTreeError(f"job {ju} has unknown timezone {tz!r}")
        retries = j.get("maxRetries", 0)
        if not isinstance(retries, int) or isinstance(retries, bool) \
                or not (0 <= retries <= CRON_MAX_RETRIES_CAP):
            raise CronTreeError(
                f"job {ju} maxRetries must be an int 0..{CRON_MAX_RETRIES_CAP}: {retries!r}"
            )
        validate_cron_expr(j.get("cron", ""))


def cron_save_tree(
    folders: list[dict[str, Any]], jobs: list[dict[str, Any]],
    *, base_version: str | None = None, expected_deletes: int | None = None,
) -> None:
    """Upsert the whole cron tree by uuid. `folders`/`jobs` use the frontend
    field names; list order becomes `position`. Existing rows are updated in
    place (so `created_at` is preserved and `updated_at`/onupdate only fires on
    real changes); rows whose uuid is absent from the incoming lists are
    deleted. No DB FKs, but keep it tidy.

    Validates the payload first (`validate_cron_tree`) and raises CronTreeError
    *before* any mutation, so a bad payload leaves the tree untouched.

    Two opt-in guards against the whole-tree-replace foot-gun (both skipped
    when their argument is None, for internal/test callers):
    - `base_version`: the `cron_tree_version()` token the caller hydrated
      with; raises CronTreeConflict (HTTP 409 upstream) when stale, so a
      second tab / external editor can't be silently clobbered.
    - `expected_deletes`: how many node deletions the caller knowingly
      performed; a save that would delete more than that (e.g. a truncated
      payload from a frontend bug) raises CronTreeError instead of wiping."""
    validate_cron_tree(folders, jobs)
    if base_version is not None and base_version != cron_tree_version():
        raise CronTreeConflict("cron tree changed since it was loaded")
    existing_f = {
        f.uuid: f
        for f in db.session.execute(sa.select(CronFolder)).scalars().all()
    }
    existing_j = {
        j.uuid: j
        for j in db.session.execute(sa.select(CronJob)).scalars().all()
    }
    if expected_deletes is not None:
        incoming = {UUID(f["id"]) for f in folders} | {UUID(j["uuid"]) for j in jobs}
        would_delete = len((set(existing_f) | set(existing_j)) - incoming)
        if would_delete > expected_deletes:
            raise CronTreeError(
                f"save would delete {would_delete} node(s) but only "
                f"{expected_deletes} deletion(s) were declared — refusing"
            )
    # Folders: update existing by uuid, insert new, delete the rest.
    seen_f: set[UUID] = set()
    for i, f in enumerate(folders):
        fu = UUID(f["id"])
        seen_f.add(fu)
        row = existing_f.get(fu)
        if row is None:
            row = CronFolder(uuid=fu)
            db.session.add(row)
        row.name = f.get("name", "")
        row.description = f.get("description", "")
        row.parent_uuid = UUID(f["parentId"]) if f.get("parentId") else None
        row.enabled = bool(f.get("enabled", True))
        row.position = i
    for fu, row in existing_f.items():
        if fu not in seen_f:
            db.session.delete(row)
    # Jobs: same upsert pattern.
    seen_j: set[UUID] = set()
    for i, j in enumerate(jobs):
        ju = UUID(j["uuid"])
        seen_j.add(ju)
        atype = j.get("type", "message")
        if atype not in CRON_ACTION_TYPES:
            atype = "message"
        row = existing_j.get(ju)
        is_new = row is None
        if row is None:
            row = CronJob(uuid=ju)
            db.session.add(row)
        prev_cron = None if is_new else row.cron_expr
        prev_tz = None if is_new else row.timezone
        prev_next = None if is_new else row.next_run_at
        row.name = j.get("name", "")
        row.enabled = bool(j.get("enabled", True))
        row.folder_uuid = UUID(j["folderId"]) if j.get("folderId") else None
        row.cron_expr = j.get("cron", "")
        tz = j.get("timezone", "localtime")
        row.timezone = tz if tz in CRON_TIMEZONES else "localtime"
        row.action_type = atype
        row.target = j.get("target", "")
        row.message = j.get("message", "")
        row.command = j.get("command", "")
        row.description = j.get("description", "")
        row.max_retries = j.get("maxRetries", 0)
        row.position = i
        # (Re)compute the next fire time when the schedule changed or isn't set;
        # the scheduler tick reads next_run_at. Unchanged jobs keep theirs.
        if is_new or row.cron_expr != prev_cron or row.timezone != prev_tz or prev_next is None:
            row.next_run_at = cron_compute_next_run(row.cron_expr, row.timezone)
    for ju, row in existing_j.items():
        if ju not in seen_j:
            db.session.delete(row)
    db.session.commit()


# ---- cron scheduler / firing ----

class CronFireError(Exception):
    """A cron job could not be fired (e.g. an empty command)."""


def cron_compute_next_run(
    cron_expr: str, tz_choice: str = "localtime", after: datetime | None = None
) -> datetime | None:
    """Next fire time (as a tz-aware UTC instant) for a cron expression evaluated
    against the job's clock: 'UTC' uses UTC, 'localtime' uses the host's local
    timezone *at this moment* (so it follows the machine when it travels).
    Returns None if the expression can't be parsed (so a bad row just never
    fires rather than crashing the scheduler)."""
    if after is None:
        after = datetime.now(UTC)
    base = after.astimezone(UTC) if tz_choice == "UTC" else after.astimezone()
    try:
        from croniter import croniter

        nxt = croniter(cron_expr, base).get_next(datetime)
    except Exception:
        return None
    return nxt.astimezone(UTC)


def _cron_job_effective_enabled(
    job: "CronJob", folders_by_uuid: dict[UUID, "CronFolder"]
) -> bool:
    """A job is live only if it is enabled and every ancestor folder is enabled
    (mirrors the UI's cronFolderEnabled). Guards against a parent cycle."""
    if not job.enabled:
        return False
    cur = job.folder_uuid
    seen: set[UUID] = set()
    while cur is not None and cur not in seen:
        seen.add(cur)
        folder = folders_by_uuid.get(cur)
        if folder is None:
            break
        if not folder.enabled:
            return False
        cur = folder.parent_uuid
    return True


def _cron_resolve_target_room(target: str) -> "Chatroom | None":
    """Resolve a message job's `target` (a chatroom uuid) to a Chatroom, or None.

    Targets are stored as chatroom uuids so a room rename can't break the job. A
    blank, non-uuid, or unknown target resolves to None (firing then falls back
    to the cron room)."""
    raw = (target or "").strip()
    if not raw:
        return None
    tid = _to_uuid(raw)
    if tid is None:
        return None
    return db.session.query(Chatroom).filter_by(uuid=tid).first()


def fire_cron_job(job: "CronJob", trigger: str = "scheduled", debug: bool = False) -> CronRun:
    """Fire one cron job now: record a CronRun, perform its action, and post a
    one-line event to the cron room. Immediate failures are caught and reported
    there; a command's own output (incl. non-zero exit) is posted to the cron
    room by the workspace-shell agent when it runs. Sets last_fired_at; does NOT
    advance next_run_at (the scheduler tick does that for scheduled fires).

    Outcome: in-process actions (message/backup) resolve the run's status
    (ok/error + finished_at + error) before returning. A command fire returns
    with status 'pending' — the workspace-shell agent writes the real outcome
    back via cron_record_run_outcome, and cron_tick sweeps runs whose
    completion never arrives (killed/hung agent) to 'error'.

    debug=True is a DRY-RUN: report what the fire *would* do without doing it.
    Messages post a '[debug] would send …' event instead of the message;
    backups resolve and report the destination without dumping; commands are
    enqueued with debug so the workspace-shell agent validates and echoes the
    argv without executing. The run row records debug=True."""
    from agent_config import agent_config

    now = datetime.now(UTC)
    run = CronRun(cron_uuid=job.uuid, trigger=trigger, debug=debug, fired_at=now)
    db.session.add(run)
    job.last_fired_at = now
    db.session.commit()

    label = job.name or "(unnamed)"
    # In-process actions resolve their outcome here; 'command' stays pending
    # until the workspace-shell agent reports back.
    outcome = "pending"
    outcome_error = ""
    try:
        if job.action_type == "command":
            cmd = (job.command or "").strip()
            if not cmd:
                raise CronFireError("no command to run")
            ws_uuid = agent_config["workspace_shell"]["uuid"]
            enqueue(ws_uuid, {
                "room_uuid": str(CRON_ROOM_UUID),
                "command_text": cmd,
                "cron_run_uuid": str(run.uuid),
                "debug": debug,
            })
            verb = "dry-run" if debug else "ran"
            post_cron_event(f'▶ {verb} "{label}" (command, {trigger}): `{cmd}`')
        elif job.action_type == "backup":
            # In-process database dump (the workspace-shell allowlist can't run
            # pg_dump/zstd or write files). The destination is the job's command
            # field, falling back to the RAINBOX_BACKUP_REPO env var. Runs
            # synchronously on the supervisor thread — fine for a local
            # single-user DB; revisit with a worker if dumps grow long.
            from backup import dump as backup_db
            from db.settings import get_setting

            # Destination: per-job command overrides the global backup.repo
            # setting (which itself resolves DB -> RAINBOX_BACKUP_REPO -> None).
            repo = (job.command or "").strip() or (get_setting("backup.repo") or "")
            if not repo:
                raise CronFireError(
                    "no backup destination (set the job's command to a directory, "
                    "the backup.repo setting, or the RAINBOX_BACKUP_REPO env var)"
                )
            # Recipients from the backup.age_recipient setting (DB -> env ->
            # default). Pass None — never [] — when unset so resolve_recipients()
            # keeps its env recipients-file fallback and fail-closed behavior.
            recips = backup_db.split_recipients(get_setting("backup.age_recipient")) or None
            if debug:
                # Dry-run: the destination + recipients resolved; nothing dumped.
                post_cron_event(
                    f'▶ dry-run "{label}" (backup, {trigger}): would back up to {repo}'
                    + (f" for {len(recips)} age recipient(s)" if recips else "")
                )
                outcome = "ok"
                run.status = outcome
                run.finished_at = datetime.now(UTC)
                db.session.commit()
                return run
            dest = backup_db.backup_database(repo, recipients=recips)
            post_cron_event(
                f'▶ backed up "{label}" ({trigger}) → {dest} '
                f"({dest.stat().st_size} bytes)"
            )
            # Optional remote upload: commit+push the (encrypted) file into the
            # backup-repo git repo. The local backup already succeeded, so an
            # upload failure is reported but doesn't fail the fire.
            from backup import remote as backup_remote

            if get_setting("backup.git_push"):
                try:
                    post_cron_event(f"  ↑ {backup_remote.git_push_backup(repo, dest)}")
                except Exception as exc:  # noqa: BLE001
                    post_cron_event(f"  ✖ upload failed: {exc}")
            # The local backup succeeded (an upload failure doesn't fail the fire).
            outcome = "ok"
        else:  # message
            text = (job.message or "").strip() or "(empty message)"
            room = _cron_resolve_target_room(job.target)
            if debug:
                # Dry-run: name the resolved destination; send nothing.
                dest_name = f"#{room.name}" if room is not None else "(cron room)"
                post_cron_event(
                    f'▶ dry-run "{label}" (message, {trigger}): '
                    f"would send {text!r} → {dest_name}"
                )
                outcome = "ok"
            elif room is not None:
                post_chat_message(room.uuid, CRON_SYSTEM_UUID, text)
                post_cron_event(f'▶ sent "{label}" (message, {trigger}) → #{room.name}')
            else:
                post_chat_message(CRON_ROOM_UUID, CRON_SYSTEM_UUID, text)
                note = f" (target {job.target!r} not found)" if (job.target or "").strip() else ""
                post_cron_event(f'▶ sent "{label}" (message, {trigger}){note}')
            outcome = "ok"
    except Exception as exc:  # noqa: BLE001 — any firing failure becomes an event line
        post_cron_event(f'✖ "{label}" failed to fire: {exc}')
        outcome = "error"
        outcome_error = str(exc)
    if outcome != "pending":
        run.status = outcome
        run.error = outcome_error
        run.finished_at = datetime.now(UTC)
    db.session.commit()
    return run


def cron_record_run_outcome(
    run_uuid: UUID | str, *, status: str, error: str = "",
    journal_id: int | None = None,
) -> None:
    """Write the final outcome of an async fire onto its CronRun row. Called by
    the workspace-shell agent (which received 'cron_run_uuid' in its payload)
    once the command finishes — ok/error per exit code, plus the journal row
    that holds the full output. A missing run row (deleted, or a payload that
    never came from cron) is a no-op.

    Also posts the consolidated ✔/✖ completion line to the cron room: the ▶
    event at fire time says the command *started*; this one says how it ended,
    so the room reads as start → output → verdict."""
    try:
        ru = run_uuid if isinstance(run_uuid, UUID) else UUID(str(run_uuid))
    except (ValueError, TypeError):
        return
    run = db.session.execute(
        sa.select(CronRun).where(CronRun.uuid == ru)
    ).scalar_one_or_none()
    if run is None:
        return
    run.status = status
    run.error = error
    run.finished_at = datetime.now(UTC)
    if journal_id is not None:
        run.journal_id = journal_id
    db.session.commit()
    job = db.session.execute(
        sa.select(CronJob).where(CronJob.uuid == run.cron_uuid)
    ).scalar_one_or_none()
    label = (job.name if job is not None else "") or "(unnamed)"
    if status == "ok":
        post_cron_event(f'✔ "{label}" completed ({run.trigger})')
    else:
        post_cron_event(
            f'✖ "{label}" failed ({run.trigger})' + (f": {error}" if error else "")
        )


def fire_cron_job_by_uuid(
    job_uuid: UUID, trigger: str = "manual", debug: bool = False
) -> CronRun | None:
    """Manual fire by uuid (the /cron 'Run now' / 'Run debug' buttons).
    Returns None if the job is gone."""
    job = db.session.execute(
        sa.select(CronJob).where(CronJob.uuid == job_uuid)
    ).scalar_one_or_none()
    if job is None:
        return None
    return fire_cron_job(job, trigger=trigger, debug=debug)


# A 'pending' run older than this is dead: the supervisor SIGKILLs a hung
# agent after ~60s, so a completion that hasn't arrived in 15 minutes never
# will. cron_tick sweeps such runs to 'error' so they can't read as in-flight
# forever (and a future skip-if-still-running guard can't deadlock on them).
CRON_RUN_PENDING_TIMEOUT = timedelta(minutes=15)


# How long after a failure a retry is still worth firing. Errors older than
# this are NOT retried — otherwise an ancient failure would refire the moment a
# restart (or newly-set max_retries) makes the tick look at it again.
CRON_RETRY_WINDOW = timedelta(minutes=10)


def cron_should_retry(job: "CronJob", now: datetime) -> bool:
    """Whether the tick should refire this job as a retry: its latest run is a
    *recent* error (within CRON_RETRY_WINDOW), and the trailing chain of
    'retry'-trigger runs is still shorter than job.max_retries. A success or a
    pending run ends/blocks the chain; the next scheduled fire starts a fresh
    one."""
    if job.max_retries <= 0:
        return False
    runs = db.session.execute(
        sa.select(CronRun).where(CronRun.cron_uuid == job.uuid)
        .order_by(CronRun.id.desc()).limit(job.max_retries + 1)
    ).scalars().all()
    if not runs or runs[0].status != "error":
        return False
    if runs[0].finished_at is None or now - runs[0].finished_at > CRON_RETRY_WINDOW:
        return False
    retries = 0
    for r in runs:
        if r.trigger != "retry":
            break
        retries += 1
    return retries < job.max_retries


def cron_job_run_in_flight(job_uuid: UUID) -> bool:
    """Whether the job's latest run is still 'pending' — an async command whose
    completion hasn't arrived yet. Bounded: the pending sweep at the top of
    every tick flips dead runs to 'error' after CRON_RUN_PENDING_TIMEOUT, so
    this can never report in-flight forever."""
    last = db.session.execute(
        sa.select(CronRun.status).where(CronRun.cron_uuid == job_uuid)
        .order_by(CronRun.id.desc()).limit(1)
    ).scalar_one_or_none()
    return last == "pending"


def cron_job_is_draft(job: "CronJob") -> bool:
    """A job whose action is still empty — being filled in gradually. The tree
    validator deliberately allows empty actions so the page can autosave drafts
    (see validate_cron_tree); the scheduler skips drafts instead of failing on
    every slot. Backups have no required field (the destination falls back to
    the backup.repo setting / env var), so they are never drafts."""
    if job.action_type == "command":
        return not (job.command or "").strip()
    if job.action_type == "message":
        return not (job.message or "").strip()
    return False


def cron_tick(now: datetime | None = None) -> int:
    """One scheduler pass (called from the supervisor loop, ~1/s). Backfills a
    missing next_run_at for enabled jobs (without firing on first sight), fires
    jobs whose next_run_at has passed, advances next_run_at to the next future
    slot — missed slots are NOT replayed (catch-up = fire at most once) — and
    sweeps long-'pending' runs whose completion will never arrive to 'error'.
    Draft jobs (empty action) advance their schedule without firing; a job
    whose previous run is still in flight skips the slot (noted in the cron
    room) instead of piling up; while the cron.paused setting is on, nothing
    fires at all (schedules don't advance, so resume behaves like
    wake-from-sleep: each due job catches up with at most one fire). Returns
    the number of jobs fired."""
    from db.settings import get_setting

    if now is None:
        now = datetime.now(UTC)
    db.session.execute(
        sa.update(CronRun)
        .where(CronRun.status == "pending",
               CronRun.fired_at < now - CRON_RUN_PENDING_TIMEOUT)
        .values(status="error", finished_at=now,
                error="no completion recorded (agent died, timed out, or the "
                      "run predates outcome tracking)")
    )
    if get_setting("cron.paused"):
        db.session.commit()  # persist the sweep; fire nothing while paused
        return 0
    folders = {
        f.uuid: f for f in db.session.execute(sa.select(CronFolder)).scalars().all()
    }
    jobs = db.session.execute(
        sa.select(CronJob).where(CronJob.enabled.is_(True))
    ).scalars().all()
    fired = 0
    for job in jobs:
        if not _cron_job_effective_enabled(job, folders):
            continue
        if job.next_run_at is None:
            # First time we see this job — schedule it; don't fire immediately.
            job.next_run_at = cron_compute_next_run(job.cron_expr, job.timezone, after=now)
            continue
        if job.next_run_at <= now:
            if cron_job_is_draft(job):
                # Draft: roll the schedule forward silently (no run row, no
                # event spam); it starts firing once its action is filled in.
                job.next_run_at = cron_compute_next_run(job.cron_expr, job.timezone, after=now)
                continue
            if cron_job_run_in_flight(job.uuid):
                # Previous fire still running: don't pile up — skip this slot
                # (no runaway processes) and note it in the cron room. Manual
                # "Run now" deliberately bypasses this (an explicit click).
                post_cron_event(
                    f'⏭ "{job.name or "(unnamed)"}" skipped: previous run still in flight'
                )
                job.next_run_at = cron_compute_next_run(job.cron_expr, job.timezone, after=now)
                continue
            fire_cron_job(job, trigger="scheduled")
            job.next_run_at = cron_compute_next_run(job.cron_expr, job.timezone, after=now)
            fired += 1
        elif not cron_job_is_draft(job) and cron_should_retry(job, now):
            # Auto-retry: the last run failed moments ago and the retry budget
            # isn't exhausted — refire now (trigger='retry' marks the chain).
            # Runs between slots, so a retry never displaces a scheduled fire;
            # drafts are excluded (a manually-fired empty action would loop).
            fire_cron_job(job, trigger="retry")
            fired += 1
    db.session.commit()
    return fired


def cron_job_health(job_uuid: UUID) -> dict[str, Any] | None:
    """Health snapshot for one job (the Job-details panel): run counts by
    outcome, last success/error, the most recent runs, and the next 3 upcoming
    fire times (pure computation — nothing stored). None if the job is gone."""
    job = db.session.execute(
        sa.select(CronJob).where(CronJob.uuid == job_uuid)
    ).scalar_one_or_none()
    if job is None:
        return None
    counts = {status: n for status, n in db.session.execute(
        sa.select(CronRun.status, sa.func.count())
        .where(CronRun.cron_uuid == job_uuid).group_by(CronRun.status)
    ).all()}
    def _last(status: str) -> datetime | None:
        return db.session.execute(
            sa.select(sa.func.max(CronRun.fired_at))
            .where(CronRun.cron_uuid == job_uuid, CronRun.status == status)
        ).scalar_one()
    recent = db.session.execute(
        sa.select(CronRun).where(CronRun.cron_uuid == job_uuid)
        .order_by(CronRun.id.desc()).limit(20)
    ).scalars().all()
    upcoming: list[str] = []
    t: datetime | None = datetime.now(UTC)
    for _ in range(3):
        t = cron_compute_next_run(job.cron_expr, job.timezone, after=t)
        if t is None:
            break
        upcoming.append(t.isoformat())
    last_ok, last_error = _last("ok"), _last("error")
    return {
        "ok_count": counts.get("ok", 0),
        "error_count": counts.get("error", 0),
        "pending_count": counts.get("pending", 0),
        "last_ok_at": last_ok.isoformat() if last_ok else None,
        "last_error_at": last_error.isoformat() if last_error else None,
        "next_runs": upcoming,
        "runs": [
            {
                "fired_at": r.fired_at.isoformat() if r.fired_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "trigger": r.trigger,
                "debug": r.debug,
                "status": r.status,
                "error": r.error,
            }
            for r in recent
        ],
    }


def seed_cron_defaults() -> None:
    """Idempotent: ensure a 'System' cron folder and a daily database-backup job
    exist (fixed uuids → safe to call on every startup). The backup job is seeded
    DISABLED with no destination; enable it and set its destination (the job's
    command field, or the RAINBOX_BACKUP_REPO env var) to start taking daily
    backups. We never re-enable or overwrite an existing row, so user edits
    stick."""
    if db.session.query(CronFolder).filter_by(uuid=SYSTEM_CRON_FOLDER_UUID).first() is None:
        db.session.add(CronFolder(
            uuid=SYSTEM_CRON_FOLDER_UUID,
            name="System",
            description="System maintenance jobs.",
            enabled=True,
            position=0,
        ))
    if db.session.query(CronJob).filter_by(uuid=BACKUP_CRON_JOB_UUID).first() is None:
        db.session.add(CronJob(
            uuid=BACKUP_CRON_JOB_UUID,
            name="Database backup",
            enabled=False,
            folder_uuid=SYSTEM_CRON_FOLDER_UUID,
            cron_expr="30 3 * * *",
            timezone="localtime",
            action_type="backup",
            command="",
            description=(
                "Dump the rainbox Postgres database to a zstd file, encrypted "
                "to a public key with age (encrypt-only — the private key stays "
                "offline for restore). Set this job's command to a backup "
                "directory (or RAINBOX_BACKUP_REPO) AND set "
                "RAINBOX_BACKUP_AGE_RECIPIENT to your age1… public key, then "
                "enable it to back up daily at 03:30 local time. To also push "
                "each backup to a remote, make the directory a git repo and set "
                "RAINBOX_BACKUP_GIT_PUSH=1."
            ),
            position=0,
        ))
    db.session.commit()
