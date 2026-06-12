"""Shared fixtures for the tools/ tests.

Everything is local (real subprocess + real Postgres); no LM Studio is involved,
so nothing skips. The DB fixture creates a throwaway room with no agent members
and tears it (and everything attached) down afterward, so these tests don't
accumulate rows in the shared dev database.
"""

from pathlib import Path

import pytest
import sqlalchemy as sa

import db
from agents.config import WORKSPACE_SHELL_UUID
from tools.workspace_policy import SHELL_CWD

# The canonical workspace_shell UUID from agent_config (kanban authority="work",
# verified=True). Must match the registry so kanban_dispatch honours its authority.
# sender_uuid is FK-less, so the agent can post without being a seeded chat_user
# or a room member.
WS_AGENT_UUID = WORKSPACE_SHELL_UUID


@pytest.fixture()
def workspace():
    """Ensure SHELL_CWD exists and give helpers to create files/dirs in it,
    cleaning up exactly what we created so the scratch dir doesn't accumulate."""
    root = Path(SHELL_CWD)
    root.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []

    def make_file(relpath: str, content: str = "") -> Path:
        p = root / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        created.append(p)
        return p

    def make_dir(relpath: str) -> Path:
        p = root / relpath
        p.mkdir(parents=True, exist_ok=True)
        created.append(p)
        return p

    yield root, make_file, make_dir

    for p in reversed(created):
        try:
            p.rmdir() if p.is_dir() else p.unlink()
        except OSError:
            pass


@pytest.fixture()
def chat_room():
    """A fresh chatroom (human creator only, no agent members) with an app
    context pushed. Yields (room, human) and deletes the room on teardown."""
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    try:
        human = db.get_human_user()
        assert human is not None
        room = db.create_chatroom("ws-shell-test", human.uuid, [])
        try:
            yield room, human
        finally:
            for table, col in (
                ("chat_message", "room_uuid"),
                ("chatroom_member", "room_uuid"),
                ("workspace_shell_state", "room_uuid"),
                ("chatroom", "uuid"),
            ):
                db.db.session.execute(
                    sa.text(f"DELETE FROM {table} WHERE {col} = :u"), {"u": room.uuid}
                )
            db.db.session.commit()
    finally:
        ctx.pop()
