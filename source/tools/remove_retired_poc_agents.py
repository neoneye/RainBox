"""Delete the dreamer/critic/verifier rows left in the database.

The three roles were an early proof of concept and are gone from the code.
Their rows are not: a `chat_user` row each (which is why the room-creation
picker still offers them, though nothing would ever answer), a model binding
each, and the demo journal rows their one demo run produced
(`{"task": "dreamer_task_0"}` and friends).

DESTRUCTIVE, and it targets whichever database DATABASE_URL points at —
which for the real rows means `rainbox_production`. Run it deliberately:

    cd source
    DATABASE_URL=postgresql+psycopg://localhost/rainbox_production \\
        venv/bin/python -m tools.remove_retired_poc_agents --apply

Without `--apply` it reports what it would do and changes nothing. It refuses
to delete unless every safety check is zero, so it cannot orphan a chat
message, a room membership or a pending inbox item. It writes the rows it
deletes to a JSON file first, so a mistake can be put back by hand.

Only these three are in scope. `query`, `query_router` and
`query_filter_router` are retired too, but they sent 1086 chat messages
between them and those messages resolve their sender through `chat_user` —
deleting those rows would blank the author of real history.
"""
import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import db

#: The retired proof-of-concept roles, by the uuid they had in agent_config.
RETIRED: dict[str, str] = {
    "dreamer": "f320e597-c571-411b-994d-65c24b62f972",
    "critic": "40c3b4b4-d883-42a9-bacf-6f77a4cd5f94",
    "verifier": "e9999acb-324b-40c1-9ec6-9047e2fb1935",
}

#: Every way another row can point at one of these agents. All must be zero
#: before anything is deleted — a non-zero count means the row is load-bearing
#: for something still readable in the UI, and the delete is off.
SAFETY_CHECKS: list[tuple[str, str]] = [
    ("chat messages sent",
     "SELECT count(*) FROM chat_message WHERE sender_uuid::text = ANY(:ids)"),
    ("room memberships",
     "SELECT count(*) FROM chatroom_member WHERE user_uuid::text = ANY(:ids)"),
    ("pending inbox items",
     "SELECT count(*) FROM inbox WHERE agent_uuid::text = ANY(:ids)"),
    ("retrieval events",
     "SELECT count(*) FROM retrieval_event WHERE agent_uuid::text = ANY(:ids)"),
]

#: What gets removed, in an order that never leaves a dangling reference.
DELETIONS: list[tuple[str, str]] = [
    ("journal", "DELETE FROM journal WHERE agent_uuid::text = ANY(:ids)"),
    ("agent_model_binding",
     "DELETE FROM agent_model_binding WHERE agent_uuid::text = ANY(:ids)"),
    ("chat_user", "DELETE FROM chat_user WHERE uuid::text = ANY(:ids)"),
]

BACKUP: list[tuple[str, str]] = [
    ("chat_user", "SELECT * FROM chat_user WHERE uuid::text = ANY(:ids)"),
    ("agent_model_binding",
     "SELECT * FROM agent_model_binding WHERE agent_uuid::text = ANY(:ids)"),
    ("journal", "SELECT * FROM journal WHERE agent_uuid::text = ANY(:ids)"),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="actually delete; without it, only report")
    parser.add_argument("--backup-dir", default=".",
                        help="where to write the pre-delete JSON dump")
    args = parser.parse_args()

    ids = list(RETIRED.values())
    app = db.make_app()
    with app.app_context():
        url = db.db.engine.url
        print(f"database: {url.database}\n")

        def scalar(sql: str) -> int:
            return db.session.execute(db.sa.text(sql), {"ids": ids}).scalar() or 0

        blocked = False
        print("safety checks (all must be 0):")
        for label, sql in SAFETY_CHECKS:
            n = scalar(sql)
            print(f"  {label:24} {n}")
            blocked = blocked or n > 0

        print("\nrows to remove:")
        counts = {name: scalar(sql.replace("DELETE FROM", "SELECT count(*) FROM"))
                  for name, sql in DELETIONS}
        for name, n in counts.items():
            print(f"  {name:24} {n}")

        if blocked:
            print("\nREFUSED: something still references these agents. "
                  "Nothing was changed.")
            return 1
        if not any(counts.values()):
            print("\nNothing to do — the rows are already gone.")
            return 0
        if not args.apply:
            print("\nDry run. Re-run with --apply to delete.")
            return 0

        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = Path(args.backup_dir) / f"retired-poc-agents-{stamp}.json"
        dump = {
            name: [{k: str(v) for k, v in row.items()}
                   for row in db.session.execute(
                       db.sa.text(sql), {"ids": ids}).mappings().all()]
            for name, sql in BACKUP
        }
        path.write_text(json.dumps(dump, indent=1), encoding="utf-8")
        print(f"\nbacked up to {path}")

        for name, sql in DELETIONS:
            result = db.session.execute(db.sa.text(sql), {"ids": ids})
            print(f"  deleted {getattr(result, 'rowcount', -1):3}  {name}")
        db.session.commit()
        print("\nDone.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
