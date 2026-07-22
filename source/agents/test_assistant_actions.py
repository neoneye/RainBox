"""Tests for the assistant's read-only actions (PR 4) and dispatch.

Each action reuses an existing rainbox surface (memory retrieval, the QueryAgent
Q&A pipeline, the workspace command policy, kanban reads) and returns an
AssistantObservation; the dispatcher owns validation, the output cap, and the
running->observed/failed trace boundary. No writes, no MCP, no generated code.
"""

import json
from uuid import UUID, uuid4

import pytest

import db
from db import AssistantStep, MemoryClaim
from agents.assistant import (
    AssistantActionContext,
    AssistantActionName,
    AssistantAgent,
    AssistantObservation,
    AssistantStepDecision,
    AssistantTurnStep,
    _action_kanban_read,
    _action_query_memory,
    _action_workspace_read_command,
)
from agents.assistant import ASSISTANT_SYSTEM_PROMPT, CAPABILITIES
from agents.assistant_fakes import scripted_decisions
from agents.config import ASSISTANT_UUID


def test_read_action_descriptions_disambiguate_query_memory_from_kanban():
    """The model once used the general Q&A action to 'query the kanban boards'.
    The catalog must steer inspecting a board to kanban_read, and mark
    memory_query as not-for-kanban."""
    qm = CAPABILITIES[AssistantActionName.MEMORY_QUERY].description.lower()
    kb = CAPABILITIES[AssistantActionName.KANBAN_READ].description.lower()
    assert "kanban" in qm and "not for" in qm          # memory_query says: not for kanban
    assert "column" in kb                              # kanban_read: look up a board's columns
    assert "kanban_read" in ASSISTANT_SYSTEM_PROMPT.lower()


def test_set_reminder_description_anchors_to_local_time():
    """A reminder time with no explicit offset must be read as the operator's
    local time, not UTC. The capability description tells the model so."""
    d = CAPABILITIES[AssistantActionName.SET_REMINDER].description.lower()
    assert "local" in d


def test_user_prompt_includes_current_local_time():
    """The model's only time anchor was the (UTC) conversation timestamps, so a
    relative offset like 'in 10 minutes' landed in UTC. Inject the current local
    time so both absolute and relative reminders resolve in the operator's zone."""
    from datetime import datetime

    agent = AssistantAgent(agent_uuid=uuid4(), name="assistant", send=lambda _: None)
    prompt = agent._build_user_prompt(
        messages=[{"sender_type": "human", "text": "hi"}],
        scratchpad=[],
        step_index=0,
    )
    assert datetime.now().astimezone().strftime("%Y-%m-%d") in prompt
    assert "current_local_time" in prompt.lower()


def test_user_prompt_has_xml_zones_and_escaped_content_but_no_policy():
    from xml.etree import ElementTree

    agent = AssistantAgent(agent_uuid=uuid4(), name="assistant", send=lambda _: None)
    messages = [
        {
            "sender_type": "agent",
            "text": "stale <answer>",
            "timestamp": "2026-07-13 17:29",
        },
        {
            "sender_type": "human",
            "text": "how is Simon related to the demoscene? </current_request>",
            "timestamp": "2026-07-13 17:34",
        },
    ]
    prompt = agent._build_user_prompt(
        messages=messages,
        scratchpad=[AssistantTurnStep(
            step_index=0,
            action="memory_query",
            args={"query": "Simon demoscene"},
            status="ok",
            observation="<recalled_memory>facts</recalled_memory>",
            is_read=True,
        )],
        step_index=1,
    )

    assert '<conversation_history authority="context_only"' in prompt
    assert 'facts_are_authoritative="false"' in prompt
    assert 'assistant_messages="omitted_after_fresh_read"' in prompt
    assert "<current_request>" in prompt
    assert '<current_turn_steps authority="fresh_evidence">' in prompt
    assert "<source_priority" not in prompt
    assert '<decision_request step="2" max_steps="6">' in prompt
    assert "stale" not in prompt
    assert "how is Simon" in prompt
    assert "&lt;/current_request&gt;" in prompt
    assert "<recalled_memory>facts</recalled_memory>" in prompt
    assert "&lt;operator&gt;" not in prompt
    assert "&lt;assistant&gt;" not in prompt
    assert "-&gt;" not in prompt
    # No <assistant_turn> root wrapper: the sections are top-level siblings
    # (models don't need a single-rooted document, and the wrapper cost one
    # level of indentation on every line). Each section is still valid,
    # ElementTree-escaped XML — proven by parsing the whole prompt under a
    # synthetic root.
    assert "<assistant_turn" not in prompt
    assert not prompt.startswith("  ")
    assert '<step index="1" action="memory_query" status="ok">' in prompt
    assert '<arguments format="json">{"query": "Simon demoscene"}</arguments>' in prompt
    assert prompt.count("<current_request>") == 1
    parsed = ElementTree.fromstring(f"<root>{prompt}</root>")
    # The task leads the prompt; the local-time anchor closes it.
    tags = [s.tag for s in parsed]
    assert tags[0] == "current_request"
    assert tags[-1] == "current_local_time"
    assert tags.index("conversation_history") < tags.index("current_turn_steps") \
        < tags.index("decision_request")
    assert "<runtime_context>" not in prompt      # wrapper dropped
    assert parsed.find("current_request") is not None


def test_source_priority_policy_is_in_system_prompt_only():
    assert '<source_priority highest_first="true">' in ASSISTANT_SYSTEM_PROMPT
    assert '<source rank="1">successful current_turn_steps observations</source>' in (
        ASSISTANT_SYSTEM_PROMPT
    )
    assert '<source rank="5">conversation_history (context only)</source>' in (
        ASSISTANT_SYSTEM_PROMPT
    )


def test_turn_event_budget_drops_whole_old_events():
    agent = AssistantAgent(agent_uuid=uuid4(), name="assistant", send=lambda _: None)
    old = AssistantTurnStep(
        step_index=0, action="memory_query", args={}, status="ok",
        observation="x" * (agent.MAX_SCRATCHPAD_CHARS + 100), is_read=True,
    )
    newest = AssistantTurnStep(
        step_index=1, action="kanban_read", args={}, status="ok",
        observation="3 tasks", is_read=True,
    )
    kept, omitted = agent._bounded_turn_events([old, newest])
    assert kept == [newest]
    assert omitted == 1


def test_turn_event_budget_keeps_oversized_newest_event_whole():
    agent = AssistantAgent(agent_uuid=uuid4(), name="assistant", send=lambda _: None)
    newest = AssistantTurnStep(
        step_index=0, action="memory_query", args={}, status="ok",
        observation="x" * (agent.MAX_SCRATCHPAD_CHARS + 100), is_read=True,
    )
    kept, omitted = agent._bounded_turn_events([newest])
    assert kept == [newest]
    assert omitted == 0


def test_turn_event_budget_preserves_events_within_budget():
    agent = AssistantAgent(agent_uuid=uuid4(), name="assistant", send=lambda _: None)
    events = [
        AssistantTurnStep(
            step_index=0, action="kanban_read", args={}, status="ok",
            observation="3 tasks", is_read=True,
        ),
        AssistantTurnStep(
            step_index=1, action="memory_query", args={"query": "x"},
            status="failed", observation="unavailable", is_read=True,
        ),
    ]
    kept, omitted = agent._bounded_turn_events(events)
    assert kept == events
    assert omitted == 0


def test_successful_read_removes_old_assistant_answers_but_keeps_operator_context():
    agent = AssistantAgent(agent_uuid=uuid4(), name="assistant", send=lambda _: None)
    messages = [
        {"sender_type": "human", "text": "earlier operator context"},
        {"sender_type": "agent", "text": "stale factual answer"},
        {"sender_type": "human", "text": "current question"},
    ]
    prompt = agent._build_user_prompt(
        messages=messages,
        scratchpad=[AssistantTurnStep(
            step_index=0, action="memory_query", args={"query": "current question"},
            status="ok", observation="fresh facts", is_read=True,
        )],
        step_index=1,
    )

    assert "earlier operator context" in prompt
    assert "stale factual answer" not in prompt
    assert 'assistant_messages="omitted_after_fresh_read"' in prompt


def test_system_prompt_forbids_claiming_unperformed_writes():
    """Run 19: the model read a task then replied 'successfully moved' with no
    kanban_task_column step. The prompt must forbid claiming a write it didn't perform."""
    p = ASSISTANT_SYSTEM_PROMPT.lower()
    assert "never tell the operator you did something" in p
    assert "reading a task is not moving it" in p


def test_system_prompt_requires_fresh_read_not_chat_history():
    """The model replied from an earlier answer in the transcript instead of
    re-querying: it reused a stored fact that had since become restricted, and
    it repeated a stale live value. The prompt must forbid answering factual or
    live-value questions from earlier messages and require a fresh read action."""
    p = ASSISTANT_SYSTEM_PROMPT.lower()
    assert "do not reuse an answer from an earlier message" in p
    assert "call the matching read action" in p
    assert "after a read action succeeds" in p
    assert "do not repeat the same read" in p
    assert "same\nargs" in p
    assert "source" in p and "current_turn_steps" in p
    assert "conversation_history (context only)" in p


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    try:
        yield app
    finally:
        db.db.session.rollback()
        ctx.pop()


@pytest.fixture
def fresh_subject() -> str:
    return f"test-{uuid4()}"


def _cleanup_subject(subject: str) -> None:
    db.db.session.query(MemoryClaim).filter(MemoryClaim.subject == subject).delete()
    db.db.session.commit()


def _ctx() -> AssistantActionContext:
    return AssistantActionContext(
        journal_id=uuid4(), room_uuid=uuid4(), agent_uuid=uuid4(), step_index=0
    )


# --- memory_query -------------------------------------------------------------


def test_query_memory_returns_relevant_fact_and_never_secret(app_ctx, fresh_subject):
    try:
        db.create_memory_claim(
            scope="global", kind="fact",
            text="the deploy host is prod-web-01",
            confidence=0.9, status="active", sensitivity="public",
            subject=fresh_subject,
        )
        db.create_memory_claim(
            scope="global", kind="fact",
            text="the deploy ssh key passphrase is swordfish",
            confidence=1.0, status="active", sensitivity="secret",
            subject=fresh_subject,
        )
        obs = _action_query_memory(_ctx(), {"query": "deploy host"})
        assert obs.ok
        assert "prod-web-01" in obs.text
        assert "swordfish" not in obs.text  # secrets are filtered before ranking
    finally:
        _cleanup_subject(fresh_subject)


def test_query_memory_with_no_matches_is_ok_and_empty(app_ctx):
    obs = _action_query_memory(_ctx(), {"query": "no such topic zzzqqq"})
    assert obs.ok
    assert obs.text  # a human-readable "nothing found" message, not a crash


def test_query_memory_includes_seed_memories_tiered(app_ctx):
    from agents.assistant import _action_query_memory
    from memory.seed_memory import SeedMemory
    def fake_seed(query, **_):
        return [SeedMemory(uuid="up-1", path="p.up", source="upstream", answer="upstream fact", score=0.7),
                SeedMemory(uuid="ov-1", path="p.ov", source="user-overlay", answer="overlay fact", score=0.65)]
    ctx = AssistantActionContext(journal_id=None, room_uuid=uuid4(), agent_uuid=ASSISTANT_UUID, step_index=0)
    obs = _action_query_memory(ctx, {"query": "anything unrelated zzz"}, _seed_retriever=fake_seed)
    assert obs.ok is True
    # user-overlay seed appears before upstream seed
    assert obs.text.index("overlay fact") < obs.text.index("upstream fact")
    # the seed uuids are present (greppable)
    assert "ov-1" in obs.text and "up-1" in obs.text
    # source tag is shown
    assert "user-overlay" in obs.text


def test_query_memory_surfaces_dynamic_handler_answer(app_ctx):
    """memory_query resolves dynamic seed handlers (project status, git status):
    a git-status handler answer must appear in the fenced block."""
    from memory.seed_memory import SeedMemory
    def fake_seed(query, *, qctx, **_):
        return [SeedMemory(uuid="dyn-git", path="dev.git", source="upstream",
                           answer="Working tree clean.", score=0.82, kind="dynamic")]
    obs = _action_query_memory(_ctx(), {"query": "what is the git status"},
                               _seed_retriever=fake_seed)
    assert obs.ok
    assert "Working tree clean." in obs.text
    assert "<recalled_memory" in obs.text


def test_query_memory_loads_seed_kb_before_retrieval(app_ctx, monkeypatch):
    """Regression: the assistant loop never loads the seed KB the way the chat
    route does, so `_entries_by_id` stayed empty and seed Q&A (e.g. family facts)
    silently returned nothing. The action must trigger `_load_kb()` +
    `_ensure_populated()` before seed retrieval, as the old query_qa action did."""
    from memory import seed_memory as qkb
    calls = []
    monkeypatch.setattr(qkb, "_load_kb", lambda: calls.append("load_kb"))
    monkeypatch.setattr(qkb, "_vector_store", lambda: "VS")
    monkeypatch.setattr(qkb, "_ensure_populated", lambda vs: calls.append(("ensure", vs)))
    monkeypatch.setattr(qkb, "retrieve_seed_answers", lambda q, *, qctx: [])
    _action_query_memory(_ctx(), {"query": "who is Gitte"})
    assert "load_kb" in calls              # KB registry loaded
    assert ("ensure", "VS") in calls       # pgvector table ensured populated


def _stub_seed_kb(monkeypatch, qkb):
    """Neutralize the seed KB's load/populate plumbing for hermetic tests."""
    monkeypatch.setattr(qkb, "_load_kb", lambda: None)
    monkeypatch.setattr(qkb, "_vector_store", lambda: "VS")
    monkeypatch.setattr(qkb, "_ensure_populated", lambda vs: None)


def _bind_model_group(monkeypatch):
    """Pretend the acting agent has a model group with one member."""
    binding = type("B", (), {"model_group_uuid": uuid4()})()
    monkeypatch.setattr(db, "get_agent_model_binding", lambda agent_uuid: binding)
    monkeypatch.setattr(
        db, "get_model_group_member_uuids", lambda group_uuid: [uuid4()])


def _seed_entries(monkeypatch, qkb, entries):
    monkeypatch.setattr(qkb, "get_entry", lambda qa_id: entries.get(qa_id))


def _score(qa_id, direct="1", indirect="1", relevancy="1"):
    return {"id": qa_id, "direct": direct, "indirect": indirect,
            "relevancy": relevancy}


def test_query_memory_recall_filter_drops_low_scores_on_a_full_list(app_ctx, monkeypatch):
    """With a full top-K list the code-side policy drops low-scored candidates:
    the LLM only scores (direct/indirect/relevancy), the keep/drop threshold
    lives in apply_filter_scores. A below-MIN_SCORE candidate with a high score
    IS recalled (the gated path would have dropped it); low-scored and unscored
    candidates are NOT; hallucinated qa_ids are ignored."""
    import agents.query_filter_router as qfr
    from memory import seed_memory as qkb
    from memory.seed_memory import Match

    _stub_seed_kb(monkeypatch, qkb)
    _bind_model_group(monkeypatch)
    _seed_entries(monkeypatch, qkb, {
        "qa-mac": {"kind": "static", "path": "identity.first_mac",
                   "_source": "user-overlay", "answer": "It was a PowerBook."},
        "qa-computer": {"kind": "static", "path": "identity.first_computer",
                        "_source": "upstream", "answer": "It was a home computer."},
        "qa-noise": {"kind": "static", "path": "other.topic",
                     "_source": "upstream", "answer": "Something off-topic."},
        "qa-food": {"kind": "static", "path": "food.pizza",
                    "_source": "upstream", "answer": "Pizza preferences."},
        "qa-unscored": {"kind": "static", "path": "other.unscored",
                        "_source": "upstream", "answer": "Never scored."},
    })
    matches = [
        Match(qa_id="qa-noise", method="semantic", score=0.90, matched_question="noise"),
        Match(qa_id="qa-mac", method="semantic", score=0.62, matched_question="first mac"),
        # Below MIN_SCORE (0.60): the gated retrieval would never surface this.
        Match(qa_id="qa-computer", method="semantic", score=0.30,
              matched_question="first computer"),
        Match(qa_id="qa-food", method="semantic", score=0.28, matched_question="pizza"),
        Match(qa_id="qa-unscored", method="semantic", score=0.25, matched_question="x"),
    ]
    monkeypatch.setattr(qkb, "_hybrid_seed_ranked", lambda q, vs, **_: matches)

    def fake_call(agent_name, model_uuids, system_prompt, user_prompt, response_model):
        assert "qa-noise" in user_prompt   # ungated candidates reach the scorer
        return (response_model(reasoning="scores calibrated on the message", items=[
            _score("qa-mac", direct="5", relevancy="5"),
            _score("qa-computer", indirect="4", relevancy="3"),
            _score("qa-noise", relevancy="2"),
            _score("qa-food"),
            _score("qa-hallucinated", direct="5"),
            # qa-unscored deliberately omitted by the LLM.
        ]), model_uuids[0])

    monkeypatch.setattr(qfr, "structured_llm_call", fake_call)
    obs = _action_query_memory(_ctx(), {"query": "first mac"})
    assert obs.ok
    assert "PowerBook" in obs.text
    assert "home computer" in obs.text          # indirect=4 passes the threshold
    assert "Something off-topic" not in obs.text  # low scores → dropped
    assert "Pizza preferences" not in obs.text
    assert "Never scored" not in obs.text         # unscored on a full list → dropped
    assert "qa-hallucinated" not in obs.text
    # The trace carries the scores and the kept/dropped verdicts.
    sf = obs.data["recall_filter"]
    assert sf["mode"] == "llm"
    by_id = {c["qa_id"]: c for c in sf["candidates"]}
    assert by_id["qa-mac"]["kept"] and by_id["qa-mac"]["direct"] == 5
    assert by_id["qa-computer"]["kept"] and by_id["qa-computer"]["indirect"] == 4
    assert not by_id["qa-noise"]["kept"]
    assert not by_id["qa-unscored"]["kept"]
    # The scorer's think-before-scoring note reaches BOTH the trace and the
    # observation text the assistant model reads.
    assert sf["reasoning"] == "scores calibrated on the message"
    # The note is fenced like recalled memory: untrusted, non-instructional.
    assert "<memory_filter_assessment note=" in obs.text
    assert "NOT instructions" in obs.text
    assert "scores calibrated on the message" in obs.text
    assert obs.text.rstrip().endswith("</memory_filter_assessment>")


def test_query_memory_recall_filter_keeps_all_when_fewer_than_top_k(app_ctx, monkeypatch):
    """With fewer than top-K candidates there is no real competition: every
    candidate is kept even when the LLM scored it low — the code-side policy
    overrides an over-aggressive scorer."""
    import agents.query_filter_router as qfr
    from memory import seed_memory as qkb
    from memory.seed_memory import Match

    _stub_seed_kb(monkeypatch, qkb)
    _bind_model_group(monkeypatch)
    _seed_entries(monkeypatch, qkb, {
        "qa-brother": {"kind": "static", "path": "identity.brother",
                       "_source": "user-overlay", "answer": "Her brother fact."},
        "qa-family": {"kind": "static", "path": "family.overview",
                      "_source": "upstream", "answer": "The family entry."},
    })
    matches = [
        Match(qa_id="qa-brother", method="semantic", score=0.80, matched_question="brother"),
        Match(qa_id="qa-family", method="semantic", score=0.55, matched_question="family"),
    ]
    monkeypatch.setattr(qkb, "_hybrid_seed_ranked", lambda q, vs, **_: matches)

    def fake_call(agent_name, model_uuids, system_prompt, user_prompt, response_model):
        return (response_model(reasoning="scores calibrated on the message", items=[
            _score("qa-brother", direct="5", relevancy="5"),
            _score("qa-family"),   # scored 1/1/1 — would drop on a full list
        ]), model_uuids[0])

    monkeypatch.setattr(qfr, "structured_llm_call", fake_call)
    obs = _action_query_memory(_ctx(), {"query": "hvem er min bror"})
    assert obs.ok
    assert "Her brother fact." in obs.text
    assert "The family entry." in obs.text   # kept despite the low scores
    sf = obs.data["recall_filter"]
    assert all(c["kept"] for c in sf["candidates"])


def test_query_memory_forwards_per_signal_budgets(app_ctx, monkeypatch):
    """The /memory/developer knobs: top_k_vector/top_k_fulltext flow through
    memory_query to the hybrid ranker unchanged — each signal fills its own
    quota, neither weighted over the other."""
    import agents.query_filter_router as qfr
    from memory import seed_memory as qkb
    from memory.seed_memory import Match

    _stub_seed_kb(monkeypatch, qkb)
    _bind_model_group(monkeypatch)
    _seed_entries(monkeypatch, qkb, {
        "qa-1": {"kind": "static", "path": "p", "_source": "upstream", "answer": "A."},
    })
    seen_budgets = []

    def fake_ranked(q, vs, **kwargs):
        seen_budgets.append((kwargs.get("top_k_vector"), kwargs.get("top_k_fulltext")))
        return [Match(qa_id="qa-1", method="fulltext", score=0.8, matched_question="q")]

    monkeypatch.setattr(qkb, "_hybrid_seed_ranked", fake_ranked)
    monkeypatch.setattr(qfr, "structured_llm_call", lambda *a, **k: (
        a[4](reasoning="budget test", items=[_score("qa-1", direct="5")]), a[1][0]))
    obs = _action_query_memory(_ctx(), {"query": "q"},
                               top_k_vector=7, top_k_fulltext=2)
    assert obs.ok
    assert seen_budgets == [(7, 2)]


def test_query_memory_claims_go_through_the_filter_too(app_ctx, monkeypatch):
    """Memory claims (the /memory store) join the seed candidates in the ONE
    filter call: a claim the scorer rates as noise is dropped from the
    observation; a relevant one stays, with its Likert scores in the trace."""
    from uuid import UUID as _UUID
    import agents.query_filter_router as qfr
    import memory.retrieval as retrieval
    from memory import seed_memory as qkb
    from memory.retrieval import RetrievedMemory

    _stub_seed_kb(monkeypatch, qkb)
    _bind_model_group(monkeypatch)
    monkeypatch.setattr(qkb, "_hybrid_seed_ranked", lambda q, vs, **_: [])
    claim_good = _UUID("11111111-1111-1111-1111-111111111111")
    claim_noise = _UUID("22222222-2222-2222-2222-222222222222")
    monkeypatch.setattr(retrieval, "retrieve_memories_hybrid", lambda *a, **k: [
        RetrievedMemory(uuid=claim_good, text="the deploy host is prod-web-01",
                        kind="fact", scope="global", confidence=0.9,
                        sensitivity="public", reason="fulltext"),
        RetrievedMemory(uuid=claim_noise, text="the operator likes pizza",
                        kind="preference", scope="global", confidence=0.9,
                        sensitivity="public", reason="fulltext"),
    ])

    def fake_call(agent_name, model_uuids, system_prompt, user_prompt, response_model):
        assert "remembered fact" in user_prompt   # claims presented to the scorer
        assert "prod-web-01" in user_prompt
        return (response_model(reasoning="one claim matches", items=[
            _score(str(claim_good), direct="5"),
            _score(str(claim_noise)),   # 1/1/1 noise
        ]), model_uuids[0])

    monkeypatch.setattr(qfr, "structured_llm_call", fake_call)
    obs = _action_query_memory(_ctx(), {"query": "deploy host"})
    assert obs.ok
    assert "prod-web-01" in obs.text
    # Both claims < 5 candidates → keep-all would keep the noise too; verify
    # via the trace that both were scored and both kept under the small-set
    # rule, so the policy (not an accident) decided.
    by_id = {c["qa_id"]: c for c in obs.data["recall_filter"]["candidates"]}
    assert by_id[str(claim_good)]["direct"] == 5
    assert by_id[str(claim_good)]["path"].startswith("claim ·")
    assert by_id[str(claim_noise)]["kept"]   # small set → kept despite noise


def test_query_memory_records_recall_verdicts_with_fifo(app_ctx, monkeypatch):
    """Live runs record one verdict per candidate — stage `used` (true
    positive) or `rejected` (false positive) with the Likert scales in
    metadata — and each stream is pruned to the recall FIFO capacity."""
    from uuid import UUID as _UUID
    import agents.query_filter_router as qfr
    import db.settings as db_settings
    import memory.retrieval as retrieval
    from db import RetrievalEvent
    from memory import seed_memory as qkb
    from memory.retrieval import RetrievedMemory

    _stub_seed_kb(monkeypatch, qkb)
    _bind_model_group(monkeypatch)
    monkeypatch.setattr(qkb, "_hybrid_seed_ranked", lambda q, vs, **_: [])
    claim_id = _UUID("88888888-8888-8888-8888-888888888888")
    monkeypatch.setattr(retrieval, "retrieve_memories_hybrid", lambda *a, **k: [
        RetrievedMemory(uuid=claim_id, text="a fifo test fact", kind="fact",
                        scope="global", confidence=0.9, sensitivity="public",
                        reason="fulltext"),
    ])
    real_get = db_settings.get_setting
    monkeypatch.setattr(
        db_settings, "get_setting",
        lambda key: 3 if key == "memory.recall_fifo_capacity" else real_get(key))

    def fake_call(agent_name, model_uuids, system_prompt, user_prompt, response_model):
        return (response_model(reasoning="noise", items=[
            _score(str(claim_id)),   # 1/1/1 — but small set → kept... see below
        ]), model_uuids[0])

    monkeypatch.setattr(qfr, "structured_llm_call", fake_call)
    try:
        for _ in range(5):
            _action_query_memory(_ctx(), {"query": "fifo probe"})
        rows = (db.db.session.query(RetrievalEvent)
                .filter_by(target_id=str(claim_id),
                           source="memory_query.filter")
                .order_by(RetrievalEvent.id.asc()).all())
        # 1 candidate < 5 → keep-all → every verdict is `used`; FIFO capacity 3.
        assert len(rows) == 3
        assert all(r.stage == "used" for r in rows)
        assert rows[-1].metadata_["direct"] == 1
        assert rows[-1].metadata_["signals"] == "fulltext"
        assert rows[-1].filter_label == "relevant"
    finally:
        db.db.session.query(RetrievalEvent).filter_by(
            target_id=str(claim_id)).delete(synchronize_session=False)
        db.db.session.commit()


def test_query_memory_dropped_claim_leaves_the_observation(app_ctx, monkeypatch):
    """On a full candidate list, a noise-scored claim is dropped from the
    observation entirely."""
    from uuid import UUID as _UUID
    import agents.query_filter_router as qfr
    import memory.retrieval as retrieval
    from memory import seed_memory as qkb
    from memory.retrieval import RetrievedMemory
    from memory.seed_memory import Match

    _stub_seed_kb(monkeypatch, qkb)
    _bind_model_group(monkeypatch)
    _seed_entries(monkeypatch, qkb, {
        f"qa-{n}": {"kind": "static", "path": f"p.{n}", "_source": "upstream",
                    "answer": f"Seed answer {n}."}
        for n in range(4)
    })
    monkeypatch.setattr(qkb, "_hybrid_seed_ranked", lambda q, vs, **_: [
        Match(qa_id=f"qa-{n}", method="semantic", score=0.8 - n * 0.1,
              matched_question=f"q{n}")
        for n in range(4)
    ])
    claim_noise = _UUID("33333333-3333-3333-3333-333333333333")
    monkeypatch.setattr(retrieval, "retrieve_memories_hybrid", lambda *a, **k: [
        RetrievedMemory(uuid=claim_noise, text="an unrelated remembered fact",
                        kind="fact", scope="global", confidence=0.9,
                        sensitivity="public", reason="fulltext"),
    ])

    def fake_call(agent_name, model_uuids, system_prompt, user_prompt, response_model):
        return (response_model(reasoning="claim is off-topic", items=[
            _score("qa-0", direct="5"),
            _score("qa-1", direct="4"),
            _score("qa-2"),
            _score("qa-3"),
            _score(str(claim_noise)),   # 1/1/1 on a full list → dropped
        ]), model_uuids[0])

    monkeypatch.setattr(qfr, "structured_llm_call", fake_call)
    obs = _action_query_memory(_ctx(), {"query": "q"})
    assert obs.ok
    assert "Seed answer 0." in obs.text
    assert "an unrelated remembered fact" not in obs.text
    by_id = {c["qa_id"]: c for c in obs.data["recall_filter"]["candidates"]}
    assert not by_id[str(claim_noise)]["kept"]


def test_recall_filter_dedicated_memory_filter_binding_wins(app_ctx, monkeypatch):
    """A bound memory_filter agent (the /agentmodel knob for scorer
    experiments) outranks every fallback: the filter scores with ITS group
    even when the router and the assistant have their own."""
    import agents.query_filter_router as qfr
    from agents.config import MEMORY_FILTER_UUID
    from memory import seed_memory as qkb
    from memory.seed_memory import Match

    _stub_seed_kb(monkeypatch, qkb)
    _seed_entries(monkeypatch, qkb, {
        "qa-1": {"kind": "static", "path": "p", "_source": "upstream", "answer": "A."},
    })
    monkeypatch.setattr(qkb, "_hybrid_seed_ranked", lambda q, vs, **_: [
        Match(qa_id="qa-1", method="semantic", score=0.8, matched_question="q")])
    filter_member = uuid4()
    filter_group, other_group = uuid4(), uuid4()

    def binding_for(agent_uuid):
        group = filter_group if agent_uuid == MEMORY_FILTER_UUID else other_group
        return type("B", (), {"model_group_uuid": group})()

    monkeypatch.setattr(db, "get_agent_model_binding", binding_for)
    monkeypatch.setattr(
        db, "get_model_group_member_uuids",
        lambda group_uuid: ([filter_member] if group_uuid == filter_group
                            else [uuid4()]))
    seen_members = []

    def fake_call(agent_name, model_uuids, system_prompt, user_prompt, response_model):
        seen_members.extend(model_uuids)
        return (response_model(reasoning="scores calibrated on the message", items=[_score("qa-1", direct="5")]), model_uuids[0])

    monkeypatch.setattr(qfr, "structured_llm_call", fake_call)
    obs = _action_query_memory(_ctx(), {"query": "q"})
    assert seen_members == [filter_member]
    assert obs.data["recall_filter"]["group_from"] == "memory_filter"


def test_recall_filter_prefers_the_query_filter_routers_model_group(app_ctx, monkeypatch):
    """The filter is a shared subsystem: when the query_filter_router has a
    model group bound, the assistant's recall filter scores with THAT group, so
    both pipelines' keep/drop decisions come from one model identity."""
    import agents.query_filter_router as qfr
    from agents.config import QUERY_FILTER_ROUTER_UUID
    from memory import seed_memory as qkb
    from memory.seed_memory import Match

    _stub_seed_kb(monkeypatch, qkb)
    _seed_entries(monkeypatch, qkb, {
        "qa-1": {"kind": "static", "path": "p", "_source": "upstream", "answer": "A."},
    })
    monkeypatch.setattr(qkb, "_hybrid_seed_ranked", lambda q, vs, **_: [
        Match(qa_id="qa-1", method="semantic", score=0.8, matched_question="q")])

    from agents.config import MEMORY_FILTER_UUID

    router_member, assistant_member = uuid4(), uuid4()
    router_group, assistant_group = uuid4(), uuid4()

    def binding_for(agent_uuid):
        if agent_uuid == MEMORY_FILTER_UUID:
            return None   # no dedicated scorer bound
        group = (router_group if agent_uuid == QUERY_FILTER_ROUTER_UUID
                 else assistant_group)
        return type("B", (), {"model_group_uuid": group})()

    monkeypatch.setattr(db, "get_agent_model_binding", binding_for)
    monkeypatch.setattr(
        db, "get_model_group_member_uuids",
        lambda group_uuid: ([router_member] if group_uuid == router_group
                            else [assistant_member]))
    seen_members = []

    def fake_call(agent_name, model_uuids, system_prompt, user_prompt, response_model):
        seen_members.extend(model_uuids)
        return (response_model(reasoning="scores calibrated on the message", items=[_score("qa-1", direct="5")]), model_uuids[0])

    monkeypatch.setattr(qfr, "structured_llm_call", fake_call)
    obs = _action_query_memory(_ctx(), {"query": "q"})
    assert seen_members == [router_member]   # not the assistant's own group
    assert obs.data["recall_filter"]["group_from"] == "query_filter_router"


def test_recall_filter_falls_back_to_own_group_when_router_unbound(app_ctx, monkeypatch):
    import agents.query_filter_router as qfr
    from agents.config import QUERY_FILTER_ROUTER_UUID
    from memory import seed_memory as qkb
    from memory.seed_memory import Match

    _stub_seed_kb(monkeypatch, qkb)
    _seed_entries(monkeypatch, qkb, {
        "qa-1": {"kind": "static", "path": "p", "_source": "upstream", "answer": "A."},
    })
    monkeypatch.setattr(qkb, "_hybrid_seed_ranked", lambda q, vs, **_: [
        Match(qa_id="qa-1", method="semantic", score=0.8, matched_question="q")])
    from agents.config import MEMORY_FILTER_UUID

    own_group = uuid4()

    def binding_for(agent_uuid):
        if agent_uuid in (QUERY_FILTER_ROUTER_UUID, MEMORY_FILTER_UUID):
            return None
        return type("B", (), {"model_group_uuid": own_group})()

    monkeypatch.setattr(db, "get_agent_model_binding", binding_for)
    monkeypatch.setattr(db, "get_model_group_member_uuids", lambda g: [uuid4()])
    monkeypatch.setattr(qfr, "structured_llm_call", lambda *a, **k: (
        a[4](reasoning="fallback test", items=[_score("qa-1", direct="5")]), a[1][0]))
    obs = _action_query_memory(_ctx(), {"query": "q"})
    assert obs.data["recall_filter"]["mode"] == "llm"
    assert obs.data["recall_filter"]["group_from"] == "own"


def test_filter_prompt_asks_for_scores_not_decisions():
    """The filter LLM scores every candidate on three Likert scales; the
    keep/drop decision is code (apply_filter_scores), not the prompt."""
    from agents.query_filter_router import FILTER_SYSTEM_PROMPT
    p = FILTER_SYSTEM_PROMPT.lower()
    assert "likert" in p
    assert "score every" in p.replace("\n", " ")
    assert "do not decide what is kept" in p.replace("\n", " ")
    assert "family" in p    # related-context example lives under `indirect`


def test_query_memory_recall_filter_falls_back_when_llm_fails(app_ctx, monkeypatch):
    """A dead filter LLM must degrade to the MIN_SCORE-gated retrieval, not to
    an empty seed block."""
    import agents.query_filter_router as qfr
    from memory import seed_memory as qkb
    from memory.seed_memory import Match, SeedMemory

    _stub_seed_kb(monkeypatch, qkb)
    _bind_model_group(monkeypatch)
    monkeypatch.setattr(qkb, "_hybrid_seed_ranked", lambda q, vs, **_: [
        Match(qa_id="qa-1", method="semantic", score=0.8, matched_question="q")])

    def boom(*_a, **_k):
        raise RuntimeError("all models in the group failed")

    monkeypatch.setattr(qfr, "structured_llm_call", boom)
    monkeypatch.setattr(qkb, "retrieve_seed_answers", lambda q, *, qctx: [
        SeedMemory(uuid="gated-1", path="p", source="upstream",
                   answer="gated fallback fact", score=0.7)])
    obs = _action_query_memory(_ctx(), {"query": "anything"})
    assert obs.ok
    assert "gated fallback fact" in obs.text
    assert obs.data["recall_filter"] == {"mode": "gated",
                                       "reason": "filter_llm_failed"}


def test_query_memory_recall_filter_skipped_without_model_group(app_ctx, monkeypatch):
    """No model group bound → straight to the gated retrieval; the ungated
    semantic ranking (which needs embeddings) must not even run."""
    from memory import seed_memory as qkb
    from memory.seed_memory import SeedMemory

    _stub_seed_kb(monkeypatch, qkb)
    monkeypatch.setattr(db, "get_agent_model_binding", lambda agent_uuid: None)
    ranked_calls = []
    monkeypatch.setattr(
        qkb, "_hybrid_seed_ranked", lambda q, vs, **_: ranked_calls.append(q) or [])
    monkeypatch.setattr(qkb, "retrieve_seed_answers", lambda q, *, qctx: [
        SeedMemory(uuid="gated-1", path="p", source="upstream",
                   answer="gated fact", score=0.7)])
    obs = _action_query_memory(_ctx(), {"query": "anything"})
    assert obs.ok
    assert "gated fact" in obs.text
    assert ranked_calls == []
    assert obs.data["recall_filter"] == {"mode": "gated",
                                       "reason": "no_model_group"}


def test_query_memory_merges_seed_and_dynamic_without_duplicate_legend(app_ctx, fresh_subject):
    """Seed + dynamic together: seed lines first, then dynamic facts, and the
    '{memory_uuid}, ...' legend appears exactly once (the dynamic block's own
    header/legend must not be re-appended)."""
    from memory.seed_memory import SeedMemory
    def fake_seed(query, **_):
        return [SeedMemory(uuid="ov-1", path="p", source="user-overlay",
                           answer="overlay fact", score=0.7)]
    try:
        db.create_memory_claim(
            scope="global", kind="fact", text="the deploy host is prod-web-01",
            confidence=0.9, status="active", sensitivity="public", subject=fresh_subject)
        obs = _action_query_memory(_ctx(), {"query": "deploy host prod"}, _seed_retriever=fake_seed)
        assert obs.ok
        assert "overlay fact" in obs.text and "prod-web-01" in obs.text   # both present
        assert obs.text.index("overlay fact") < obs.text.index("prod-web-01")  # seed before dynamic
        assert obs.text.count("{memory_uuid}") == 1   # the legend is not duplicated
    finally:
        _cleanup_subject(fresh_subject)


def test_query_memory_observation_is_fenced_when_memories_present(app_ctx, fresh_subject):
    """The memory_query observation must wrap retrieved facts in a recalled_memory
    fence so they enter the model context as untrusted reference data."""
    try:
        db.create_memory_claim(
            scope="global", kind="fact", text="the deploy host is prod-web-01 fenced",
            confidence=0.9, status="active", sensitivity="public", subject=fresh_subject)
        obs = _action_query_memory(_ctx(), {"query": "deploy host fenced"})
        assert obs.ok
        assert "<recalled_memory" in obs.text
        assert obs.text.rstrip().endswith("</recalled_memory>")
    finally:
        _cleanup_subject(fresh_subject)


# --- workspace_read_command ---------------------------------------------------


def test_workspace_read_command_allows_safe_command(app_ctx):
    obs = _action_workspace_read_command(_ctx(), {"command": "pwd"})
    assert obs.ok
    assert obs.data.get("exit_code") == 0


def test_workspace_read_command_blocks_forbidden_command(app_ctx):
    obs = _action_workspace_read_command(_ctx(), {"command": "python -c 'print(1)'"})
    assert obs.ok is False
    assert "blocked" in obs.text.lower()


def test_workspace_read_command_blocks_mutation_command(app_ctx):
    obs = _action_workspace_read_command(_ctx(), {"command": "rm -rf foo"})
    assert obs.ok is False
    assert "blocked" in obs.text.lower()


# --- kanban_read --------------------------------------------------------------


def test_kanban_read_returns_board_state_without_appending_events(app_ctx):
    board = db.kanban_create_board("Assistant read board", "desc")
    bu = UUID(board["uuid"])
    todo = board["columns"][0]["uuid"]
    task = {
        "uuid": str(uuid4()), "columnUuid": todo,
        "title": "ship the thing", "description": "", "agentUuid": None,
    }
    db.kanban_save_board(bu, {**board, "tasks": [task]})
    task_uuid = UUID(task["uuid"])
    events_before = db.kanban_task_events(task_uuid) or []
    try:
        obs = _action_kanban_read(_ctx(), {"board_uuid": str(bu)})
        assert obs.ok
        document = json.loads(obs.text)   # the LLM JSON twin, not markdown
        assert document["boardId"] == str(bu)
        assert any(t["title"] == "ship the thing"
                   for c in document["columns"] for t in c["tasks"])
        events_after = db.kanban_task_events(task_uuid) or []
        assert len(events_after) == len(events_before), "read must not write events"
    finally:
        db.kanban_delete_board(bu)


def test_kanban_read_without_args_lists_boards(app_ctx):
    board = db.kanban_create_board("Listable board", "desc")
    bu = UUID(board["uuid"])
    try:
        obs = _action_kanban_read(_ctx(), {})
        assert obs.ok
        assert "Listable board" in obs.text
    finally:
        db.kanban_delete_board(bu)


def test_kanban_read_board_list_shows_folder_tree(app_ctx):
    """The no-arg listing is JSON preserving the folder tree: folders carry
    their uuids and children, and a board sits under its folder."""
    folder = db.kanban_create_folder("Projects read test")
    sub = db.kanban_create_folder("Active", UUID(folder["uuid"]))
    board = db.kanban_create_board("Tree board", folder_uuid=UUID(sub["uuid"]))
    try:
        obs = _action_kanban_read(_ctx(), {})
        assert obs.ok
        tree = json.loads(obs.text)["tree"]
        f_node = next(n for n in tree if n.get("folderId") == folder["uuid"])
        assert f_node["name"] == "Projects read test"
        s_node = next(n for n in f_node["children"]
                      if n.get("folderId") == sub["uuid"])
        b_node = next(n for n in s_node["children"]
                      if n.get("boardId") == board["uuid"])
        assert b_node["name"] == "Tree board" and b_node["taskCount"] == 0
    finally:
        db.kanban_delete_board(UUID(board["uuid"]))
        db.kanban_delete_folder(UUID(sub["uuid"]))
        db.kanban_delete_folder(UUID(folder["uuid"]))


def test_kanban_read_unknown_board_is_blocked(app_ctx):
    obs = _action_kanban_read(_ctx(), {"board_uuid": str(uuid4())})
    assert obs.ok is False


def test_kanban_read_by_task_uuid_returns_task_and_events(app_ctx):
    board = db.kanban_create_board("Task read board", "desc")
    bu = UUID(board["uuid"])
    todo = board["columns"][0]["uuid"]
    task = {"uuid": str(uuid4()), "columnUuid": todo,
            "title": "fix the bug", "description": "acceptance: tests pass"}
    db.kanban_save_board(bu, {**board, "tasks": [task]})
    tu = UUID(task["uuid"])
    db.kanban_append_event(tu, "comment", actor="human", detail="please prioritize")
    try:
        obs = _action_kanban_read(_ctx(), {"task_uuid": str(tu)})
        assert obs.ok is True
        detail = json.loads(obs.text)
        assert detail["taskId"] == str(tu) and detail["title"] == "fix the bug"
        assert detail["columnName"] == "To do"
        assert [c["name"] for c in detail["boardColumns"]] == \
            ["To do", "In progress", "Done"]
        assert any(e["detail"] == "please prioritize"
                   for e in detail["recentEvents"])
        assert obs.data["task_uuid"] == str(tu)
    finally:
        db.kanban_delete_board(bu)


def test_kanban_read_unknown_task_is_blocked(app_ctx):
    obs = _action_kanban_read(_ctx(), {"task_uuid": str(uuid4())})
    assert obs.ok is False


# --- find_uuid ----------------------------------------------------------------


def test_find_uuid_resolves_a_fragment(app_ctx):
    """A partial uuid resolves to the entity with kind, full uuid, and
    parents — the weak-LLM path to a correct uuid without guessing."""
    from agents.assistant import _action_find_uuid

    board = db.kanban_create_board("Find action board")
    try:
        prefix = UUID(board["uuid"]).hex[:10]
        obs = _action_find_uuid(_ctx(), {"query": prefix})
        assert obs.ok
        matches = json.loads(obs.text)["matches"]
        (m,) = [m for m in matches if m["uuid"] == board["uuid"]]
        assert m["kind"] == "kanban board" and m["match"] == "substring"
    finally:
        db.kanban_delete_board(UUID(board["uuid"]))


def test_find_uuid_refuses_short_query(app_ctx):
    from agents.assistant import _action_find_uuid

    obs = _action_find_uuid(_ctx(), {"query": "7d"})
    assert obs.ok is False and "at least" in obs.text


# --- argument validation ------------------------------------------------------


def test_validate_rejects_unsupported_kanban_args(app_ctx):
    """kanban_read takes optional board_uuid / task_uuid; an unknown arg must be a
    traceable validation failure, not a silent fall-through to 'list all boards'."""
    agent = AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None)
    bad = AssistantStepDecision(
        reason="x", action=AssistantActionName.KANBAN_READ,
        args={"column_uuid": str(uuid4())},  # not a kanban_read arg
    )
    err = agent._validate_decision(bad)
    assert err and "column_uuid" in err
    # board_uuid, task_uuid, and empty args are all accepted.
    for ok_args in ({"board_uuid": str(uuid4())}, {"task_uuid": str(uuid4())}, {}):
        ok = AssistantStepDecision(
            reason="x", action=AssistantActionName.KANBAN_READ, args=ok_args)
        assert agent._validate_decision(ok) is None


# --- dispatch through the loop ------------------------------------------------


@pytest.fixture
def room(app_ctx):
    human = db.get_human_user()
    assert human is not None
    chatroom = db.create_chatroom(f"act-test-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    msg = db.post_chat_message(chatroom.uuid, human.uuid, "do a read")
    try:
        yield chatroom.uuid, msg.uuid
    finally:
        from db import AssistantRun
        db.db.session.query(AssistantRun).filter(
            AssistantRun.room_uuid == chatroom.uuid
        ).delete()
        db.db.session.query(db.Chatroom).filter(
            db.Chatroom.uuid == chatroom.uuid
        ).delete()
        db.db.session.commit()


def _agent() -> AssistantAgent:
    return AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None)


def _decision(action: AssistantActionName, **args) -> AssistantStepDecision:
    return AssistantStepDecision(reason="step", action=action, args=args)


def _steps_for(run_id):
    return (
        db.db.session.query(AssistantStep)
        .filter(AssistantStep.run_uuid == run_id)
        .order_by(AssistantStep.id)
        .all()
    )


def test_loop_dispatches_read_action_then_replies(room):
    room_uuid, message_uuid = room
    agent = _agent()
    agent._decide_next_step = scripted_decisions(
        _decision(AssistantActionName.MEMORY_QUERY, query="anything"),
        _decision(AssistantActionName.REPLY, message="All set."),
    )
    result = agent.handle(uuid4(), {"room_uuid": str(room_uuid), "message_uuid": str(message_uuid)})

    assert result["status"] == "finished"
    steps = _steps_for(result["assistant_run_uuid"])
    # One row per step: the read step settles running->observed in place, the
    # reply is a single terminal row.
    assert [s.phase for s in steps] == ["observed", "final"]
    observed = steps[0]
    assert observed.action == "memory_query"
    assert observed.observation_preview is not None


def test_loop_does_not_dispatch_identical_successful_read_twice(room):
    """If the model repeats the exact same read, the loop should not replay the
    action. The earlier observation is already in the scratchpad; the repeat gets
    a steering observation that tells the model to reply."""
    room_uuid, message_uuid = room
    agent = _agent()
    calls = []

    def fake_dispatch(ctx, decision):
        calls.append((ctx.step_index, decision.action.value, dict(decision.args)))
        return AssistantObservation(ok=True, text="remembered fact: Simon used demos")

    agent._dispatch_action = fake_dispatch
    agent._decide_next_step = scripted_decisions(
        _decision(AssistantActionName.MEMORY_QUERY, query="Simon relation to demoscene"),
        _decision(AssistantActionName.MEMORY_QUERY, query="Simon relation to demoscene"),
        _decision(AssistantActionName.REPLY, message="Simon used demos."),
    )

    result = agent.handle(uuid4(), {"room_uuid": str(room_uuid), "message_uuid": str(message_uuid)})

    assert result["status"] == "finished"
    assert calls == [
        (0, "memory_query", {"query": "Simon relation to demoscene"})
    ]
    steps = _steps_for(result["assistant_run_uuid"])
    assert [s.phase for s in steps] == ["observed", "observed", "final"]
    assert "remembered fact: Simon used demos" in (steps[1].observation_preview or "")
    assert "already completed this exact read" in (steps[1].observation_preview or "")


def test_loop_does_not_dispatch_identical_failed_action_twice(room):
    """A failed action with unchanged args cannot produce new evidence, so the
    host should reject its replay before dispatch and steer the next decision."""
    room_uuid, message_uuid = room
    agent = _agent()
    calls = []

    def fake_dispatch(ctx, decision):
        calls.append((ctx.step_index, decision.action.value, dict(decision.args)))
        return AssistantObservation(ok=False, text="backend unavailable")

    agent._dispatch_action = fake_dispatch
    agent._decide_next_step = scripted_decisions(
        _decision(AssistantActionName.MEMORY_QUERY, query="Simon demoscene"),
        _decision(AssistantActionName.MEMORY_QUERY, query="Simon demoscene"),
        _decision(AssistantActionName.REPLY, message="I could not retrieve that fact."),
    )

    result = agent.handle(
        uuid4(), {"room_uuid": str(room_uuid), "message_uuid": str(message_uuid)}
    )

    assert result["status"] == "finished"
    assert calls == [(0, "memory_query", {"query": "Simon demoscene"})]
    steps = _steps_for(result["assistant_run_uuid"])
    assert [s.phase for s in steps] == ["failed", "failed", "final"]
    assert "already failed earlier" in (steps[1].observation_preview or "")


def test_loop_records_failed_action_and_continues(room):
    """A blocked/forbidden read becomes a failed step (trace-before-action: the
    running row is committed first), and the loop continues to a terminal reply."""
    room_uuid, message_uuid = room
    agent = _agent()
    agent._decide_next_step = scripted_decisions(
        _decision(AssistantActionName.WORKSPACE_READ_COMMAND, command="rm -rf /"),
        _decision(AssistantActionName.REPLY, message="Could not do that."),
    )
    result = agent.handle(uuid4(), {"room_uuid": str(room_uuid), "message_uuid": str(message_uuid)})

    assert result["status"] == "finished"
    steps = _steps_for(result["assistant_run_uuid"])
    # The blocked read opens a running row (committed before the action) that
    # settles in place to failed; then a terminal reply row.
    assert [s.phase for s in steps] == ["failed", "final"]
    failed = steps[0]
    assert failed.action == "workspace_read_command"
    assert failed.error and "blocked" in failed.error.lower()


# --- memory_query truncation (per-fact cap + overall budget + uuid full-fetch) ---


def test_query_memory_truncates_large_facts_and_tags_them(app_ctx):
    """A fact longer than the per-fact cap is shortened and tagged truncate1200
    so the model knows it is partial and can fetch the full text by uuid."""
    from memory.seed_memory import SeedMemory
    big = "x" * 3000
    def fake_seed(query, *, qctx, **_):
        return [SeedMemory(uuid="u-big", path="p", source="user-overlay",
                           answer=big, score=0.9, kind="static")]
    obs = _action_query_memory(_ctx(), {"query": "q"}, _seed_retriever=fake_seed)
    assert "u-big, seed/user-overlay, p, truncate1200:" in obs.text
    assert ("x" * 1200) in obs.text and ("x" * 1201) not in obs.text  # capped at 1200
    assert obs.data["truncated"] == 1  # count of shortened facts


def test_query_memory_data_splits_counts_and_tags_dynamic(app_ctx):
    """The trace data separates QA static / QA dynamic / memory counts; a
    dynamic seed entry (a live handler) carries a `dynamic` tag in its line,
    and every seed line carries the entry's `path` so answers whose text alone
    is ambiguous (two uptime strings, a load average) stay tellable apart."""
    from memory.seed_memory import SeedMemory
    def fake_seed(query, *, qctx, **_):
        return [SeedMemory(uuid="u-stat", path="project.overview", source="upstream",
                           answer="a static fact", score=0.9, kind="static"),
                SeedMemory(uuid="u-dyn", path="system.uptime_host", source="upstream",
                           answer="14 cron jobs", score=0.9, kind="dynamic")]
    obs = _action_query_memory(_ctx(), {"query": "q"}, _seed_retriever=fake_seed)
    assert obs.data["qa_static"] == 1
    assert obs.data["qa_dynamic"] == 1
    assert "memory" in obs.data and "memory_uuids" not in obs.data
    # the dynamic seed carries the `dynamic` tag; the static one does not
    assert "u-dyn, seed/upstream, dynamic, system.uptime_host: 14 cron jobs" in obs.text
    assert "u-stat, seed/upstream, project.overview: a static fact" in obs.text


def test_query_memory_seed_without_path_omits_the_path_tag(app_ctx):
    """A seed entry with no `path` renders without a trailing path tag (no
    dangling comma)."""
    from memory.seed_memory import SeedMemory
    def fake_seed(query, *, qctx, **_):
        return [SeedMemory(uuid="u-nopath", path="", source="upstream",
                           answer="a fact", score=0.9, kind="static")]
    obs = _action_query_memory(_ctx(), {"query": "q"}, _seed_retriever=fake_seed)
    assert "u-nopath, seed/upstream: a fact" in obs.text


def test_query_memory_omits_tail_and_notes_it(app_ctx):
    """When more capped facts than fit the budget are retrieved, the tail is
    dropped at a fact boundary (never mid-word) and the omission is noted."""
    from memory.seed_memory import SeedMemory
    def fake_seed(query, *, qctx, **_):
        return [SeedMemory(uuid=f"u{i}", path="p", source="user-overlay",
                           answer="y" * 1200, score=0.9, kind="static") for i in range(15)]
    obs = _action_query_memory(_ctx(), {"query": "q"}, _seed_retriever=fake_seed)
    assert obs.data["omitted"] > 0
    assert "omitted" in obs.text.lower()


def test_query_memory_uuid_returns_full_seed_entry(app_ctx, monkeypatch):
    """memory_query with a uuid returns that one entry in full, untruncated —
    the escape hatch for a fact memory_query shortened."""
    from memory import seed_memory as qkb
    big = "z" * 3000
    monkeypatch.setattr(qkb, "_load_kb", lambda: None)
    monkeypatch.setattr(qkb, "_entries_by_id",
                        {"u1": {"kind": "static", "answer": big, "_source": "user-overlay"}})
    monkeypatch.setattr(qkb, "_unlocked_shields", lambda: set())
    obs = _action_query_memory(_ctx(), {"uuid": "u1"})
    assert obs.data.get("matched") is True
    assert big in obs.text  # full text, not shortened


def test_query_memory_uuid_respects_shield(app_ctx, monkeypatch):
    from memory import seed_memory as qkb
    monkeypatch.setattr(qkb, "_load_kb", lambda: None)
    monkeypatch.setattr(qkb, "_entries_by_id",
                        {"u1": {"kind": "static", "answer": "secret", "shield": "locked", "_source": "user-overlay"}})
    monkeypatch.setattr(qkb, "_unlocked_shields", lambda: set())
    obs = _action_query_memory(_ctx(), {"uuid": "u1"})
    assert obs.data.get("matched") is False
    assert "secret" not in obs.text


def test_system_prompt_explains_truncate_and_uuid_fetch():
    p = ASSISTANT_SYSTEM_PROMPT.lower()
    assert "truncate" in p
    assert '"uuid"' in p or "uuid" in p
