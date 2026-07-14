"""Phase 0 acceptance spine — regression cases for behaviour that already
exists, written *before* the assistant skeleton so the harness can fail if a
later PR breaks them.

These are the "can be written before Phase 1" cases from the eval catalog in
docs/proposals/2026-06-19-improvements-v2.md:

- memory exact answer + forbidden secret memory not injected (one end-to-end
  retrieval scenario, distinct from the granular unit tests in
  memory/test_retrieval.py): a query returns the relevant *public* fact and
  never the *secret* one — the "Filter before rank" contract.
- query project status: the read-only QueryAgent handler path that
  `memory_query` reuses still returns text.

No LM Studio dependency. The memory cases use the deterministic token-overlap
retriever; the project-status case calls a read-only handler directly.

Trace-shape acceptance cases (two-step trace, step cap, failed action) are
co-developed with the assistant in PRs 2-3 — they cannot exist before the loop
and the trace tables do.
"""

from uuid import uuid4

import pytest

import db
from db import MemoryClaim

from agents.query_handlers import QueryContext, get_git_status
from memory.retrieval import retrieve_memories


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    try:
        yield app
    finally:
        ctx.pop()


@pytest.fixture
def fresh_subject() -> str:
    return f"test-{uuid4()}"


def _cleanup(subject: str) -> None:
    db.db.session.query(MemoryClaim).filter(
        MemoryClaim.subject == subject
    ).delete()
    db.db.session.commit()


def test_memory_answer_returns_public_fact_and_never_the_secret(
    app_ctx, fresh_subject
):
    """Acceptance: a query about a topic returns the relevant active public
    fact and must never surface a secret fact on the same topic. This is the
    'Filter before rank' contract, exercised end-to-end through the public
    retrieve_memories() entry point."""
    try:
        public = db.create_memory_claim(
            scope="global", kind="fact",
            text="the office wifi network name is acme-guest",
            confidence=0.9, status="active", sensitivity="public",
            subject=fresh_subject,
        )
        db.create_memory_claim(
            scope="global", kind="fact",
            text="the office wifi password is hunter2",
            confidence=1.0, status="active", sensitivity="secret",
            subject=fresh_subject,
        )

        out = retrieve_memories("office wifi", agent_uuid=None, room_uuid=None)
        uuids = {m.uuid for m in out}
        texts = " ".join(m.text for m in out)

        assert public.uuid in uuids, "the relevant public fact should be retrieved"
        assert "hunter2" not in texts, "a secret fact must never be injected"
    finally:
        _cleanup(fresh_subject)


def test_project_status_handler_returns_text(app_ctx):
    """Acceptance: the read-only QueryAgent project-status handler path that
    memory_query reuses still returns a non-empty string and does not raise.
    Reflects whatever the working tree happens to be — we assert it is a
    usable answer, not a specific repo state."""
    ctx = QueryContext(
        room_uuid=uuid4(),
        query="what is the current git status?",
        payload={},
        agent_uuid=uuid4(),
    )
    answer = get_git_status(ctx)
    assert isinstance(answer, str)
    assert answer.strip(), "git status handler should return a non-empty answer"
