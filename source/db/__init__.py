"""Database facade.

This package's submodules are db.models, db.queue, db.model_config, db.chat,
db.assistant, db.conversation, db.memory, db.feedback, db.eval, db.cron,
db.kanban, db.settings. This `__init__` is the facade that re-exports their
public names
so callers can simply `import db`; it also owns the app/db lifecycle
(make_app, init_db, reset_demo_data).
"""
import logging
import os
from pathlib import Path

import sqlalchemy as sa
from flask import Flask

from db.models import *  # noqa: F401,F403  re-export db, models, constants, label helpers, psycopg_dsn
from db.queue import *  # noqa: F401,F403  re-export queue ops (enqueue, take_item, ...)
from db.model_config import *  # noqa: F401,F403  re-export model config/overrides/groups/bindings
from db.chat import *  # noqa: F401,F403  re-export chat rooms/users/messages/NOTIFY/seed helpers
from db.chat import _chat_event_payload  # noqa: F401  db/test_chat_streaming.py imports this private helper
from db.assistant import *  # noqa: F401,F403  re-export assistant trace ops (start_assistant_run, append_assistant_step, finish_run, ...)
from db.conversation import *  # noqa: F401,F403  re-export conversation_run ops (manager CAS, stop, …)
from db.memory import *  # noqa: F401,F403  re-export memory claim/evidence ops
from db.feedback import *  # noqa: F401,F403  re-export feedback + retrieval-telemetry ops
from db.eval import *  # noqa: F401,F403  re-export eval case/run/result + promotion ops
from db.cron import *  # noqa: F401,F403  re-export cron tree/scheduler/firing ops
from db.kanban import *  # noqa: F401,F403  re-export kanban board/task/agent ops
from db.git import *  # noqa: F401,F403  re-export git tree ops + repo inspection
from db.settings import *  # noqa: F401,F403  re-export app_setting registry/accessors

logger = logging.getLogger(__name__)


def make_app() -> Flask:
    """Build a Flask app wired to the Postgres database.

    Used by webapp.py directly and by main.py/agent.py to obtain an
    app context they can push for db.session access."""
    _root = Path(__file__).parent.parent  # source/
    # Flask(__name__) inside a package resolves root_path to db/, so the
    # static folder must be anchored explicitly to the source root.
    app = Flask(__name__, static_folder=str(_root / "static"))
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
        "DATABASE_URL", DEFAULT_DATABASE_URL
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    # Flask-Admin uses flashed messages (and thus the session), which require a
    # secret key. This is a local single-user demo, so a fixed dev default is
    # fine; override with SECRET_KEY for anything exposed beyond localhost.
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "rainbox-dev")
    db.init_app(app)
    return app


def _migrate_ollama_native_args() -> None:
    """Rewrite legacy OpenAI-compat-shaped Ollama config arguments to the native
    `Ollama(...)` shape: api_base→base_url (stripping /v1), timeout→
    request_timeout, drop api_key/is_chat_model. Runs in init_db; idempotent (a
    row already in native shape produces no change)."""
    changed = False
    for cfg in db.session.query(ModelConfig).filter(ModelConfig.provider == "ollama"):
        a = dict(cfg.arguments or {})
        before = dict(a)
        if "api_base" in a:
            base = str(a.pop("api_base") or "")
            if base.endswith("/v1"):
                base = base[: -len("/v1")]
            a["base_url"] = base.rstrip("/") or "http://127.0.0.1:11434"
        if "timeout" in a:
            a["request_timeout"] = a.pop("timeout")
        a.pop("api_key", None)
        a.pop("is_chat_model", None)
        if a != before:
            cfg.arguments = a
            changed = True
    if changed:
        db.session.commit()


def _migrate_cron_message_targets() -> None:
    """Convert legacy message-job targets (a chatroom NAME, optionally '#'-prefixed)
    to the chatroom uuid, so renaming a room can't break a cron job. Idempotent:
    a value that is already a uuid (or empty) is left alone; a name with no
    matching room is cleared (firing then falls back to the cron room)."""
    from uuid import UUID

    changed = False
    for job in db.session.query(CronJob).filter(CronJob.action_type == "message"):
        tgt = (job.target or "").strip()
        if not tgt:
            continue
        try:
            UUID(tgt)
            continue  # already a uuid
        except (ValueError, TypeError):
            pass
        room = db.session.query(Chatroom).filter_by(name=tgt.lstrip("#")).first()
        job.target = str(room.uuid) if room else ""
        changed = True
    if changed:
        db.session.commit()


def _column_exists(table: str, column: str) -> bool:
    return db.session.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name=:t AND column_name=:c"
        ),
        {"t": table, "c": column},
    ).first() is not None


def _add_column_if_missing(table: str, column: str, ddl: str) -> None:
    """ALTER TABLE only when the column is genuinely absent. A plain
    `ADD COLUMN IF NOT EXISTS` is logically idempotent but still takes an
    ACCESS EXCLUSIVE lock on EVERY startup — and init_db runs in every
    process (webapp, each spawned agent, each pytest), so an unconditional
    ALTER deadlocks against any session merely holding an open read
    transaction on the table. The information_schema pre-check makes the
    steady state lock-free. IF NOT EXISTS stays in the guarded DDL so two
    processes racing through a legacy DB's first post-upgrade startup can
    both pass the pre-check without the loser crashing."""
    if not _column_exists(table, column):
        db.session.execute(
            sa.text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {ddl}"))


def _backfill_chatroom_positions() -> None:
    """One-time: give existing chatrooms a `position` reflecting their current
    visible order (created_at, then id) so adding the column doesn't visibly
    reshuffle the left panel. Idempotent: runs only while every row still shares
    one position value (the freshly-migrated state — COUNT(DISTINCT position) <=
    1); once positions diverge (a real reorder, or this backfill) it's a no-op,
    so a later reorder is never clobbered."""
    distinct = db.session.execute(
        sa.text("SELECT COUNT(DISTINCT position) FROM chatroom")
    ).scalar()
    if (distinct or 0) > 1:
        return
    db.session.execute(sa.text(
        "UPDATE chatroom c SET position = sub.rn FROM ("
        "  SELECT id, (ROW_NUMBER() OVER (ORDER BY created_at, id) - 1) AS rn"
        "  FROM chatroom"
        ") sub WHERE c.id = sub.id"
    ))
    db.session.commit()


def _migrate_journal_id_to_uuid() -> None:
    """Convert `journal.id` from an integer autoincrement to a uuid, remapping
    every loose `journal_id` reference (there are no FK constraints). Idempotent:
    runs only while `journal.id` is still an integer; a no-op once it's a uuid,
    and a no-op on a fresh DB (create_all already builds the uuid column).

    Existing history is preserved: each journal row gets a stable uuid and the
    referencing columns are remapped to it by join before the int id is dropped.
    """
    jid_type = db.session.execute(
        sa.text("SELECT data_type FROM information_schema.columns "
                "WHERE table_name='journal' AND column_name='id'")
    ).scalar()
    if jid_type not in ("integer", "bigint"):
        return  # already uuid (or no journal table yet)
    logger.info("migrating journal.id from %s to uuid (preserving history)", jid_type)

    # 1. Mint a uuid for every existing journal row.
    db.session.execute(sa.text("ALTER TABLE journal ADD COLUMN IF NOT EXISTS uuid_id uuid"))
    db.session.execute(sa.text(
        "UPDATE journal SET uuid_id = gen_random_uuid() WHERE uuid_id IS NULL"))

    # 2. Remap each loose referencing column to the new uuid, by join.
    for table, col in (
        ("cron_run", "journal_id"),
        ("conversation_run", "last_speaker_journal_id"),
        ("retrieval_event", "journal_id"),
        ("assistant_run", "journal_id"),
    ):
        if not _column_exists(table, col):
            continue
        db.session.execute(sa.text(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col}__u uuid"))
        db.session.execute(sa.text(
            f"UPDATE {table} t SET {col}__u = j.uuid_id FROM journal j "
            f"WHERE j.id = t.{col} AND t.{col} IS NOT NULL"))
        db.session.execute(sa.text(f"ALTER TABLE {table} DROP COLUMN {col}"))
        db.session.execute(sa.text(f"ALTER TABLE {table} RENAME COLUMN {col}__u TO {col}"))

    # 3. Swap the journal primary key over to the uuid column.
    db.session.execute(sa.text("ALTER TABLE journal DROP CONSTRAINT IF EXISTS journal_pkey"))
    db.session.execute(sa.text("ALTER TABLE journal DROP COLUMN id"))
    db.session.execute(sa.text("ALTER TABLE journal RENAME COLUMN uuid_id TO id"))
    db.session.execute(sa.text("ALTER TABLE journal ALTER COLUMN id SET DEFAULT gen_random_uuid()"))
    db.session.execute(sa.text("ALTER TABLE journal ALTER COLUMN id SET NOT NULL"))
    db.session.execute(sa.text("ALTER TABLE journal ADD PRIMARY KEY (id)"))

    # 4. Recreate the dropped-with-the-column indexes.
    db.session.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS journal_by_agent ON journal (agent_uuid, id)"))
    db.session.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_assistant_run_journal_id "
        "ON assistant_run (journal_id)"))
    db.session.commit()


def _constraint_def(name: str) -> str | None:
    row = db.session.execute(
        sa.text("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname=:n"),
        {"n": name},
    ).first()
    return row[0] if row else None


def init_db(app: Flask) -> None:
    with app.app_context():
        # pgvector must exist before create_all() builds the memory_embedding
        # table's vector column. Idempotent; the operator's DB already uses it
        # for the Q&A store.
        db.session.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
        db.session.commit()
        db.create_all()
        _migrate_journal_id_to_uuid()
        # Idempotent column additions for tables that pre-date the column.
        # create_all() never ALTERs existing tables; this catches DBs that
        # were created before size_bytes was introduced. All additions go
        # through _add_column_if_missing so a fully-migrated DB starts up
        # without taking a single exclusive lock (see its docstring).
        _add_column_if_missing("model_config", "size_bytes",
                               "size_bytes BIGINT")
        _add_column_if_missing("model_config", "provider",
                               "provider TEXT NOT NULL DEFAULT 'lm_studio'")
        _add_column_if_missing("model_config", "display_name",
                               "display_name TEXT NOT NULL DEFAULT ''")
        if _constraint_def("model_config_model_name_key") is not None:
            db.session.execute(
                sa.text(
                    "ALTER TABLE model_config "
                    "DROP CONSTRAINT model_config_model_name_key"
                )
            )
        db.session.execute(
            sa.text(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "model_config_provider_model_name_key "
                "ON model_config (provider, model_name)"
            )
        )
        _add_column_if_missing("chat_message", "content_type",
                               "content_type TEXT NOT NULL DEFAULT 'markdown'")
        _add_column_if_missing("chat_message", "kind",
                               "kind TEXT NOT NULL DEFAULT 'message'")
        _add_column_if_missing("chat_message", "streaming",
                               "streaming BOOLEAN NOT NULL DEFAULT FALSE")
        # model_group capability constraints migrated from two booleans
        # (requires_*) to two tri-state text columns (*_constraint). The
        # add/backfill/drop of the OLD bool columns is guarded so it runs ONLY
        # while they still exist — otherwise every startup would re-add them
        # (ADD ... IF NOT EXISTS re-creates a dropped column) and re-drop them,
        # leaking column slots toward Postgres's hard 1600-column-per-table cap.
        _add_column_if_missing(
            "model_group", "function_calling_constraint",
            "function_calling_constraint TEXT NOT NULL DEFAULT 'dont_care'")
        _add_column_if_missing(
            "model_group", "structured_output_constraint",
            "structured_output_constraint TEXT NOT NULL DEFAULT 'dont_care'")
        _add_column_if_missing(
            "model_group", "reasoning_constraint",
            "reasoning_constraint TEXT NOT NULL DEFAULT 'dont_care'")
        has_old_caps = _column_exists("model_group", "requires_function_calling")
        if has_old_caps:
            # One-time backfill: a previously-required capability becomes
            # "must_have". A False bool can't distinguish "don't care" from "must
            # not have", so it maps to the safe default "dont_care".
            db.session.execute(sa.text(
                "ALTER TABLE model_group ADD COLUMN IF NOT EXISTS "
                "requires_structured_output BOOLEAN NOT NULL DEFAULT FALSE"
            ))
            db.session.execute(sa.text(
                "UPDATE model_group SET function_calling_constraint='must_have' "
                "WHERE requires_function_calling=TRUE"
            ))
            db.session.execute(sa.text(
                "UPDATE model_group SET structured_output_constraint='must_have' "
                "WHERE requires_structured_output=TRUE"
            ))
            db.session.execute(sa.text(
                "ALTER TABLE model_group DROP COLUMN IF EXISTS requires_function_calling"
            ))
            db.session.execute(sa.text(
                "ALTER TABLE model_group DROP COLUMN IF EXISTS requires_structured_output"
            ))
        _add_column_if_missing("eval_run", "is_baseline",
                               "is_baseline BOOLEAN NOT NULL DEFAULT FALSE")
        _add_column_if_missing("cron_folder", "description",
                               "description TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing("cron_job", "timezone",
                               "timezone TEXT NOT NULL DEFAULT 'localtime'")
        # Widen the action_type check to admit the in-process 'backup' and
        # 'memory_sync' actions (create_all() never ALTERs an existing
        # constraint, so older DBs still carry a narrower CHECK). Re-created only
        # while the current definition lacks the newest value ('memory_sync') —
        # the drop+add pair takes an exclusive lock, so the steady state skips it.
        _action_def = _constraint_def("cron_job_action_type_check")
        if _action_def is None or "memory_sync" not in _action_def:
            db.session.execute(
                sa.text("ALTER TABLE cron_job DROP CONSTRAINT IF EXISTS cron_job_action_type_check")
            )
            db.session.execute(
                sa.text(
                    "ALTER TABLE cron_job ADD CONSTRAINT cron_job_action_type_check "
                    "CHECK (action_type IN ('message','command','backup','memory_sync'))"
                )
            )
        # cron_run outcome tracking (status/finished_at/error) added after the
        # table's first cut. Pre-existing rows get 'pending' and are swept to
        # 'error' ("no completion recorded") by cron_tick — honest for rows
        # that predate outcome tracking.
        _add_column_if_missing("cron_run", "status",
                               "status TEXT NOT NULL DEFAULT 'pending'")
        _add_column_if_missing("cron_run", "finished_at",
                               "finished_at TIMESTAMPTZ")
        _add_column_if_missing("cron_run", "error",
                               "error TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing("cron_job", "max_retries",
                               "max_retries INTEGER NOT NULL DEFAULT 0")
        # kanban_task claim/lease columns (milestone 2) added after the table's
        # first cut: claimed_by is the current worker, holding a lease until
        # claim_expires_at.
        _add_column_if_missing("kanban_task", "claimed_by", "claimed_by UUID")
        _add_column_if_missing("kanban_task", "claimed_at",
                               "claimed_at TIMESTAMPTZ")
        _add_column_if_missing("kanban_task", "claim_expires_at",
                               "claim_expires_at TIMESTAMPTZ")
        # kanban board folders (the left-panel tree, added after the board
        # table's first cut). New table kanban_board_folder is created by
        # create_all() above; the placement column is back-filled here.
        _add_column_if_missing("kanban_board", "folder_uuid", "folder_uuid UUID")
        # Chat-folder columns (added after chatroom's first cut). New table
        # chatroom_folder is created by create_all() above.
        _add_column_if_missing("chatroom", "folder_uuid", "folder_uuid UUID")
        _add_column_if_missing("chatroom", "position",
                               "position INTEGER NOT NULL DEFAULT 0")
        _backfill_chatroom_positions()
        # The write-intent → producing-step identity pointer (the sole step
        # reference). Additive nullable column on DBs that predate it; fresh DBs
        # get the FK via create_all.
        _add_column_if_missing("assistant_write_intent", "step_uuid", "step_uuid UUID")
        # The former (run_id, step_index) soft pointer is superseded by step_uuid.
        # Drop it where it still exists (guarded so a fully-migrated DB starts up
        # without an ACCESS EXCLUSIVE lock — same rationale as _add_column_if_missing).
        if _column_exists("assistant_write_intent", "step_index"):
            db.session.execute(
                sa.text("ALTER TABLE assistant_write_intent DROP COLUMN IF EXISTS step_index"))
        _status_def = _constraint_def("cron_run_status_check")
        if _status_def is None or "error" not in _status_def:
            db.session.execute(
                sa.text("ALTER TABLE cron_run DROP CONSTRAINT IF EXISTS cron_run_status_check")
            )
            db.session.execute(
                sa.text(
                    "ALTER TABLE cron_run ADD CONSTRAINT cron_run_status_check "
                    "CHECK (status IN ('pending','ok','error'))"
                )
            )
        # Skills retrieval telemetry reuses retrieval_event; widen the
        # target_type/stage CHECKs to admit skill 'considered'/'injected' rows.
        _rt_target = _constraint_def("ck_retrieval_event_target_type")
        if _rt_target is None or "skill" not in _rt_target:
            db.session.execute(
                sa.text("ALTER TABLE retrieval_event DROP CONSTRAINT IF EXISTS ck_retrieval_event_target_type")
            )
            db.session.execute(
                sa.text(
                    "ALTER TABLE retrieval_event ADD CONSTRAINT ck_retrieval_event_target_type "
                    "CHECK (target_type IN ('qa_entry','memory_claim','skill'))"
                )
            )
        _rt_stage = _constraint_def("ck_retrieval_event_stage")
        if _rt_stage is None or "injected" not in _rt_stage:
            db.session.execute(
                sa.text("ALTER TABLE retrieval_event DROP CONSTRAINT IF EXISTS ck_retrieval_event_stage")
            )
            db.session.execute(
                sa.text(
                    "ALTER TABLE retrieval_event ADD CONSTRAINT ck_retrieval_event_stage "
                    "CHECK (stage IN ('retrieved','accepted','rejected','used',"
                    "'downvoted','considered','injected'))"
                )
            )
        # Phase 6 adds the transient 'stopping' assistant_run status.
        _ar_status = _constraint_def("assistant_run_status_check")
        if _ar_status is not None and "stopping" not in _ar_status:
            db.session.execute(
                sa.text("ALTER TABLE assistant_run DROP CONSTRAINT IF EXISTS assistant_run_status_check")
            )
            db.session.execute(
                sa.text(
                    "ALTER TABLE assistant_run ADD CONSTRAINT assistant_run_status_check "
                    "CHECK (status IN ('running','stopping','finished','stopped','failed','killed'))"
                )
            )
        db.session.commit()
        _migrate_ollama_native_args()
        _migrate_cron_message_targets()
        # Seed an (unassigned) model binding for each code-defined agent.
        from agents.config import agent_config

        ensure_agent_model_bindings([entry["uuid"] for entry in agent_config.values()])
        seed_chat_defaults()
        seed_cron_defaults()
        reconcile_app_settings()


def reset_demo_data() -> None:
    """Wipe inbox and journal — useful between demo runs."""
    db.session.execute(sa.delete(Inbox))
    db.session.execute(sa.delete(Journal))
    db.session.commit()


