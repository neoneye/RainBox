"""db.find_uuid: the cross-table fuzzy uuid lookup — exact, substring
(prefix/suffix/middle), and typo-tolerant fuzzy matching, with kind, parent
chain, and deep-link url in every match."""

from uuid import UUID

import pytest

import db


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
def world(app_ctx):
    """A kanban folder > board > task chain to look up; removed afterwards."""
    folder = db.kanban_create_folder("find-uuid folder")
    board = db.kanban_create_board("find-uuid board",
                                   folder_uuid=UUID(folder["uuid"]))
    task = db.kanban_create_task(
        UUID(board["uuid"]), UUID(board["columns"][0]["uuid"]),
        title="find-uuid task")
    try:
        yield {"folder": folder, "board": board, "task": task}
    finally:
        db.kanban_delete_board(UUID(board["uuid"]))
        db.kanban_delete_folder(UUID(folder["uuid"]))


def _hits(matches, uuid_str):
    return [m for m in matches if m["uuid"] == uuid_str]


def test_exact_match_with_parents_and_url(world):
    task_uuid = world["task"]["uuid"]
    matches = db.find_uuid(task_uuid)
    (m,) = _hits(matches, task_uuid)
    assert m["kind"] == "kanban task" and m["match"] == "exact"
    assert m["name"] == "find-uuid task"
    assert m["url"] == f"/kanban?id={task_uuid}"
    # Parents inner → outer: column, board, folder.
    kinds = [p["kind"] for p in m["parents"]]
    assert kinds == ["kanban column", "kanban board", "kanban folder"]
    assert m["parents"][1]["uuid"] == world["board"]["uuid"]
    assert m["parents"][2]["name"] == "find-uuid folder"


def test_exact_match_ignores_dashes_braces_case(world):
    bu = world["board"]["uuid"]
    for spelling in (bu.upper(), "{" + bu + "}", bu.replace("-", ""),
                     f"urn:uuid:{bu}"):
        (m,) = _hits(db.find_uuid(spelling), bu)
        assert m["match"] == "exact", spelling


def test_prefix_suffix_and_middle_fragments(world):
    bu = world["board"]["uuid"]
    hex32 = UUID(bu).hex
    for fragment in (hex32[:8], hex32[-8:], hex32[10:20]):
        (m,) = _hits(db.find_uuid(fragment), bu)
        assert m["match"] == "substring", fragment


def test_fuzzy_match_tolerates_a_typo(world):
    bu = world["board"]["uuid"]
    hex32 = UUID(bu).hex
    # Flip one character of a 16-char fragment to a hex value it isn't.
    typo = ("0" if hex32[8] != "0" else "1") + hex32[9:24]
    matches = db.find_uuid(typo)
    (m,) = _hits(matches, bu)
    assert m["match"] == "fuzzy" and m["confidence"] < 1.0


def test_short_query_is_refused(app_ctx):
    with pytest.raises(ValueError):
        db.find_uuid("7de")


def test_no_match_returns_empty_list(app_ctx):
    # 12 hex chars that exist nowhere (fuzzy threshold keeps randoms out).
    assert db.find_uuid("fedcba987654fedcba987654fedcba98") == []


def test_column_links_to_its_board(world):
    col_uuid = world["board"]["columns"][0]["uuid"]
    (m,) = _hits(db.find_uuid(col_uuid), col_uuid)
    assert m["kind"] == "kanban column"
    assert m["url"] == f"/kanban?id={world['board']['uuid']}"
    assert m["parents"][0]["kind"] == "kanban board"


def test_prefix_match_ranks_above_middle_match(world):
    """For the same fragment, the uuid that STARTS with it sorts above one
    that merely contains it — people quote the beginning of a uuid."""
    fragment = "2f70dead"
    prefix_uuid = "2f70dead-0000-4000-8000-000000000001"
    middle_uuid = "00000000-0000-2f70-dead-000000000002"
    assert fragment in UUID(middle_uuid).hex
    assert not UUID(middle_uuid).hex.startswith(fragment)
    bu = UUID(world["board"]["uuid"])
    fresh = db.kanban_load_board(bu)
    todo = fresh["columns"][0]["uuid"]
    fresh["tasks"] += [
        {"uuid": middle_uuid, "columnUuid": todo, "title": "middle",
         "description": "", "agentUuid": None},
        {"uuid": prefix_uuid, "columnUuid": todo, "title": "prefix",
         "description": "", "agentUuid": None},
    ]
    db.kanban_save_board(bu, fresh)
    matches = db.find_uuid(fragment)
    uuids = [m["uuid"] for m in matches]
    assert uuids.index(prefix_uuid) < uuids.index(middle_uuid)


def test_mention_in_chat_message_text(app_ctx):
    """A uuid that exists ONLY inside a chat message's text (no row carries
    it) is still found: the message is reported as a 'mention', linking to
    its room — even when the query fragment spans a dash boundary."""
    ghost = "3adf3498-fa22-4bbb-8bbb-123456789abc"
    user = db.ChatUser(name="find-test human", user_type="human")
    db.db.session.add(user)
    db.db.session.commit()
    room = db.create_chatroom("Q&A find test", user.uuid, [])
    try:
        db.post_chat_message(room.uuid, user.uuid,
                             f"the run for task {ghost} failed twice")
        matches = db.find_uuid("adf3498-fa22")  # spans the first dash
        m = next(x for x in matches if x["kind"] == "chat message")
        assert m["match"] == "mention"
        assert m["name"].startswith("the run for task")  # the message excerpt
        assert m["url"] == f"/chat?id={room.uuid}"
        assert any(p["kind"] == "chat room" and p["name"] == "Q&A find test"
                   for p in m["parents"])
    finally:
        db.delete_chatroom(room.uuid)
        db.db.session.delete(user)
        db.db.session.commit()


def test_mention_in_task_event_reports_the_task(world):
    """A uuid quoted in a task EVENT detail surfaces the task that owns the
    event — the entity a caller can act on, not the log line."""
    ghost = "aaaabbbb-cccc-4ddd-8eee-ffff00001111"
    task_uuid = UUID(world["task"]["uuid"])
    db.kanban_append_event(task_uuid, "note", actor="human",
                           detail=f"duplicated from `{ghost}`")
    matches = db.find_uuid("aaaabbbbcccc")
    m = next(x for x in matches if x["kind"] == "kanban task")
    assert m["match"] == "mention" and m["uuid"] == str(task_uuid)


def test_direct_match_is_not_duplicated_as_mention(world):
    """A task whose description quotes its OWN uuid appears once, as the
    direct match — the mention pass dedupes against it."""
    task_uuid = world["task"]["uuid"]
    bu = UUID(world["board"]["uuid"])
    fresh = db.kanban_load_board(bu)
    for t in fresh["tasks"]:
        if t["uuid"] == task_uuid:
            t["description"] = f"my own id is {task_uuid}"
    db.kanban_save_board(bu, fresh)
    matches = db.find_uuid(task_uuid)
    hits = _hits(matches, task_uuid)
    assert len(hits) == 1 and hits[0]["match"] == "exact"


def test_exact_ranks_above_substring(world):
    """The full uuid of one entity is also a query; its exact hit sorts
    before any coincidental substring hits elsewhere."""
    matches = db.find_uuid(world["task"]["uuid"])
    assert matches[0]["uuid"] == world["task"]["uuid"]
    assert matches[0]["match"] == "exact"
