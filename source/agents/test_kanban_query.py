"""The kanban_query action finds boards, folders, and tasks BY NAME — exact,
substring, and fuzzy (typo-tolerant) — as a ranked candidate list whose uuids
feed the other kanban actions."""

import json
from uuid import UUID, uuid4

import pytest

import db
from agents.assistant import (
    CAPABILITIES,
    AssistantActionContext,
    AssistantActionName,
    _action_kanban_query,
)
from agents.config import ASSISTANT_UUID


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
def kanban(app_ctx):
    """A folder holding a board with a distinctive task title."""
    folder = db.kanban_create_folder("Weekly chores")
    board = db.kanban_create_board("Deployment checklist")
    bu = UUID(board["uuid"])
    fresh = db.kanban_load_board(bu)
    fresh["folderId"] = folder["uuid"]
    fresh["tasks"] = [{"uuid": str(uuid4()),
                       "columnUuid": fresh["columns"][0]["uuid"],
                       "title": "Rotate the api keys", "description": "d"}]
    db.kanban_save_board(bu, fresh)
    tree = db.kanban_load_tree()
    for b in tree["boards"]:
        if b["uuid"] == board["uuid"]:
            b["folderId"] = folder["uuid"]
    db.kanban_save_tree(tree["folders"], tree["boards"],
                        base_version=tree["version"])
    try:
        yield {"folder": folder, "board": db.kanban_load_board(bu)}
    finally:
        db.kanban_delete_board(bu)
        db.kanban_delete_folder(UUID(folder["uuid"]))


def _ctx():
    return AssistantActionContext(
        journal_id=None, room_uuid=uuid4(),
        agent_uuid=ASSISTANT_UUID, step_index=0,
    )


def _candidates(obs):
    return json.loads(obs.text)["candidates"]


def test_capability_is_prompt_exposed_read():
    cap = CAPABILITIES[AssistantActionName.KANBAN_QUERY]
    assert cap.read is True
    assert cap.write is False
    assert cap.prompt_exposed is True
    assert cap.required_args == ("query",)


def test_exact_board_name_ranks_first(kanban):
    obs = _action_kanban_query(_ctx(), {"query": "Deployment checklist"})
    assert obs.ok is True
    top = _candidates(obs)[0]
    assert top["kind"] == "kanban board"
    assert top["uuid"] == kanban["board"]["uuid"]
    assert top["match"] == "exact"
    assert top["url"] == f"/kanban?id={kanban['board']['uuid']}"


def test_substring_is_case_insensitive_and_beats_fuzzy(kanban):
    obs = _action_kanban_query(_ctx(), {"query": "DEPLOY"})
    assert obs.ok is True
    hits = [c for c in _candidates(obs) if c["uuid"] == kanban["board"]["uuid"]]
    assert hits and hits[0]["match"] == "substring"


def test_fuzzy_matches_a_typod_folder_name(kanban):
    obs = _action_kanban_query(_ctx(), {"query": "weekly chors"})
    assert obs.ok is True
    hits = [c for c in _candidates(obs) if c["uuid"] == kanban["folder"]["uuid"]]
    assert hits and hits[0]["kind"] == "kanban folder"
    assert hits[0]["match"] == "fuzzy"


def test_task_candidate_carries_column_and_board_parents(kanban):
    obs = _action_kanban_query(_ctx(), {"query": "rotate the api keys"})
    assert obs.ok is True
    top = _candidates(obs)[0]
    assert top["kind"] == "kanban task"
    assert top["match"] == "exact"
    parent_kinds = [p["kind"] for p in top["parents"]]
    assert parent_kinds[:2] == ["kanban column", "kanban board"]
    assert "kanban folder" in parent_kinds
    assert any(p["name"] == "Deployment checklist" for p in top["parents"])


def test_multiple_candidates_are_ranked_best_first(kanban):
    # "chores" hits the folder (substring); a fuzzy word hit may trail it.
    obs = _action_kanban_query(_ctx(), {"query": "chores"})
    assert obs.ok is True
    scores = [c["confidence"] for c in _candidates(obs)]
    assert scores == sorted(scores, reverse=True)
    assert _candidates(obs)[0]["uuid"] == kanban["folder"]["uuid"]


def test_too_short_query_is_refused(app_ctx):
    obs = _action_kanban_query(_ctx(), {"query": "x"})
    assert obs.ok is False
    assert "too short" in obs.text


def test_no_match_points_to_kanban_read(app_ctx):
    obs = _action_kanban_query(_ctx(), {"query": "zqxwvutsrq"})
    assert obs.ok is True
    assert obs.data["count"] == 0
    assert "kanban_read" in obs.text
