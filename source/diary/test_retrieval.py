"""Diary reads end to end on the sandbox DB (proposal §7–§8): validation,
isolation on every route, literal/search/timeline/read behavior, grouping,
cursors, exact budgets and telemetry. Synthetic content only."""

import re
import shutil
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa

import db
from diary.action import DiaryRequestError, diary_query, validate_request
from diary.ingest import sync_source
from diary.retrieval import DiaryContext
from db.test_diary import manifest_for

FIXTURE = Path(__file__).resolve().parent.parent / "data" / "diary_fixture"
BUDGET = 3500


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    try:
        yield app
    finally:
        db.session.rollback()
        ctx.pop()


class World:
    def __init__(self, root: Path, room: uuid.UUID, agent: uuid.UUID, src):
        self.root, self.room, self.agent, self.src = root, room, agent, src

    def ctx(self, **k) -> DiaryContext:
        base = dict(room_uuid=self.room, agent_uuid=self.agent, models_local=True)
        base.update(k)
        return DiaryContext(**base)

    def q(self, budget=BUDGET, **args):
        return diary_query(args, self.ctx(), budget=budget)

    def write(self, rel: str, text: str) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def resync(self) -> None:
        report = sync_source(self.src.uuid)
        assert report.quarantined == 0, report


@pytest.fixture
def world(app_ctx, tmp_path):
    root = (tmp_path / "input")
    shutil.copytree(FIXTURE, root, ignore=shutil.ignore_patterns("README.md"))
    root = root.resolve()
    room, agent = uuid.uuid4(), uuid.uuid4()
    db.session.add(db.Chatroom(uuid=room, name="diary-read", created_by=uuid.uuid4()))
    db.session.commit()
    name = f"diary-read-{uuid.uuid4().hex[:12]}"
    src, _ = db.diary_register_source(manifest_for(root, room, name))
    w = World(root, room, agent, src)
    try:
        yield w
    finally:
        db.session.rollback()
        db.session.execute(sa.delete(db.DiaryCursor).where(db.DiaryCursor.room_uuid == room))
        db.session.execute(sa.delete(db.RetrievalEvent).where(db.RetrievalEvent.room_uuid == room))
        db.session.execute(sa.update(db.DiaryFile).where(db.DiaryFile.source_uuid == src.uuid)
                           .values(current_generation_uuid=None))
        db.session.execute(sa.delete(db.DiarySource).where(db.DiarySource.uuid == src.uuid))
        db.session.execute(sa.delete(db.Chatroom).where(db.Chatroom.uuid == room))
        db.session.commit()


@pytest.fixture
def live(world):
    """The fixture tree synced and enabled."""
    world.resync()
    db.diary_set_enabled(world.src.uuid, True)
    return world


def citations(obs) -> list[str]:
    return re.findall(r"diary:[0-9a-f-]{36}:\d+-\d+", obs.text)


def assert_fenced(obs, budget=BUDGET):
    assert len(obs.text) <= budget
    if "<diary_passages" in obs.text:
        assert obs.text.count("<diary_passages") == 1 and obs.text.count("</diary_passages>") == 1


# --- validation (no DB) ---------------------------------------------------------------


@pytest.mark.parametrize("args, fragment", [
    ({"mode": "search"}, "requires"),
    ({"mode": "search", "query": ""}, "requires"),
    ({"mode": "search", "query": "x" * 513}, "1-512"),
    ({"mode": "fly"}, "mode"),
    ({"mode": "search", "query": "x", "cursor": "y"}, "not allowed"),
    ({"mode": "search", "query": "x", "surprise": 1}, "unknown"),
    ({"mode": "timeline", "date_from": "2027-03-12"}, "requires"),
    ({"mode": "timeline", "date_from": "2027-03-12", "date_to": "2027-03-11"}, "before"),
    ({"mode": "timeline", "date_from": "2027-13-01", "date_to": "2027-13-02"}, "ISO"),
    ({"mode": "timeline", "date_from": "2020-01-01", "date_to": "2027-01-01"}, "366"),
    ({"mode": "continue", "cursor": "nope"}, "cursor"),
    ({"mode": "read"}, "requires"),
])
def test_validation_rejects(args, fragment):
    with pytest.raises(DiaryRequestError, match=fragment):
        validate_request(args)


def test_validation_accepts_inclusive_single_day_and_untrimmed_literal():
    r = validate_request({"mode": "timeline", "date_from": "2027-03-12", "date_to": "2027-03-12"})
    assert r.date_from == r.date_to == date(2027, 3, 12)
    assert validate_request({"mode": "literal", "query": "  spaced "}).query == "  spaced "


# --- isolation --------------------------------------------------------------------------


def test_disabled_source_is_unavailable_on_every_route(world):
    world.resync()
    for args in ({"mode": "search", "query": "physiotherapy"},
                 {"mode": "literal", "query": "Physiotherapy"},
                 {"mode": "timeline", "date_from": "2027-03-12", "date_to": "2027-03-12"}):
        obs = world.q(**args)
        assert not obs.ok and obs.data["error"] == "unavailable"


def test_other_room_other_agent_and_remote_models(live):
    other = DiaryContext(room_uuid=uuid.uuid4(), agent_uuid=live.agent, models_local=True)
    assert diary_query({"mode": "search", "query": "physiotherapy"}, other, budget=BUDGET).data["error"] == "unavailable"
    remote = live.ctx(models_local=False)
    assert diary_query({"mode": "search", "query": "physiotherapy"}, remote, budget=BUDGET).data["error"] == "unavailable"
    m = manifest_for(live.root, live.room, live.src.name, agent_uuid=str(uuid.uuid4()))
    db.diary_register_source(m)
    db.diary_set_enabled(live.src.uuid, True)
    assert live.q(mode="search", query="physiotherapy").data["error"] == "unavailable"


def test_allow_remote_models_opts_in(live):
    db.diary_register_source(manifest_for(live.root, live.room, live.src.name, allow_remote_models=True))
    db.diary_set_enabled(live.src.uuid, True)
    obs = diary_query({"mode": "search", "query": "physiotherapy"}, live.ctx(models_local=False), budget=BUDGET)
    assert obs.ok and citations(obs)


def test_excluded_file_is_invisible_on_every_route(live):
    obs = live.q(mode="literal", query="Edit::VSpace#move_left")
    cite = citations(obs)[0]
    db.diary_exclude(live.src.uuid, "archive/ChangeLog.txt")
    assert "Edit::VSpace" not in live.q(mode="literal", query="Edit::VSpace#move_left").text
    assert "Edit::VSpace" not in live.q(mode="search", query="Edit::VSpace#move_left").text
    assert live.q(mode="read", citation=cite).data["error"] == "not_found"
    tl = live.q(mode="timeline", date_from="2027-07-27", date_to="2027-07-27")
    assert "Edit::VSpace" not in tl.text


# --- literal --------------------------------------------------------------------------------


def test_literal_finds_exact_match_with_citation(live):
    obs = live.q(mode="literal", query="Edit::VSpace#move_left")
    assert obs.ok and "Edit::VSpace#move_left" in obs.text
    assert "2027-07-27" in obs.text and "author kreese" in obs.text
    assert obs.data["coverage"]["enumeration"] == "complete"
    assert_fenced(obs)


def test_literal_treats_sql_wildcards_literally(live):
    live.write("current/2028.txt", "20280101\n09h00\nsaved 100%_done here\n\n10h00\nsaved 100 done\n")
    live.resync()
    obs = live.q(mode="literal", query="100%_done")
    assert obs.text.count("--- current/2028.txt") == 1


def test_literal_crosses_chunk_boundaries(live):
    long = "20280102\n09h00\n" + ("a" * 90 + "\n") * 7 + "needle-spanning" + "b" * 50 + "\n"
    long += ("c" * 90 + "\n") * 3
    live.write("current/2028.txt", long)
    live.resync()
    obs = live.q(mode="literal", query="needle-spanning")
    assert "needle-spanning" in obs.text


def test_literal_prompt_spelling_fallback(live):
    live.write("current/2028.txt", "20280103\n09h00\nuse Vec<u8> for the buffer\n")
    live.resync()
    obs = live.q(mode="literal", query="Vec‹u8›")
    assert obs.ok and "Vec‹u8›" in obs.text      # rendered through the fence escape
    assert "--- current/2028.txt" in obs.text


def test_date_filter_applies_before_the_cap(live):
    body = "".join(f"2028020{d}\n09h00\nalpha {d}\n\n" for d in range(1, 10))
    body += "".join(f"20280210\n{h:02d}h00\nalpha extra {h}\n\n" for h in range(0, 23))
    body += "20280211\n09h00\nalpha last\n"
    live.write("current/2028.txt", body)
    live.resync()
    obs = live.q(mode="literal", query="alpha", date_from="2028-02-11", date_to="2028-02-11")
    assert "alpha last" in obs.text and "alpha extra" not in obs.text


def test_literal_pages_without_skips_or_duplicates(live):
    body = "".join(f"20280301\n{h:02d}h00\nrepeat marker {h}\n\n" for h in range(0, 24))
    live.write("current/2028.txt", body)
    live.resync()
    seen = []
    obs = live.q(budget=900, mode="literal", query="repeat marker")
    while True:
        seen += re.findall(r"repeat marker (\d+)", obs.text)
        if not obs.data["next_cursor"]:
            break
        obs = live.q(budget=900, mode="continue", cursor=obs.data["next_cursor"])
    assert [int(x) for x in seen] == list(range(24))


# --- search ------------------------------------------------------------------------------------


def test_search_groups_identical_routine_entries(live):
    obs = live.q(mode="search", query="physiotherapy exercises")
    assert obs.text.count("Physiotherapy exercises.") == 1
    assert "(same text also on 2027-03-12)" in obs.text
    assert "2027-03-13 09h30–10h00" in obs.text          # most recent occurrence cited
    assert obs.data["coverage"]["enumeration"] == "not_applicable"


def test_search_identifier_hash_prefix(live):
    obs = live.q(mode="search", query="what broke in 3fa9c2e")
    assert "3fa9c2e1" in obs.text


def test_search_is_multilingual_by_lexeme(live):
    obs = live.q(mode="search", query="Rücken")
    assert "Mein Rücken" in obs.text


def test_search_no_match_is_successful_empty(live):
    obs = live.q(mode="search", query="zeppelinxyz")
    assert obs.ok and "no matching passages" in obs.text and "not proof" in obs.text


def test_search_cursor_freezes_ranking(live):
    first = live.q(budget=700, mode="search", query="the")
    assert first.data["next_cursor"]
    cur = db.session.get(db.DiaryCursor, uuid.UUID(first.data["next_cursor"]))
    assert cur.position["candidates"] and cur.position["index"] >= 1
    second = live.q(budget=700, mode="continue", cursor=first.data["next_cursor"])
    assert set(citations(first)).isdisjoint(citations(second))


# --- timeline -----------------------------------------------------------------------------------


def test_timeline_one_inclusive_day(live):
    obs = live.q(mode="timeline", date_from="2027-03-12", date_to="2027-03-12")
    assert "Woke at" in obs.text and "Mein Rücken" in obs.text
    assert "2027-03-13" not in obs.text


def test_timeline_reads_newest_first_changelog_chronologically(live):
    obs = live.q(mode="timeline", date_from="2027-07-25", date_to="2027-07-27")
    t = obs.text
    assert t.index("started the undo rewrite") < t.index("re-enabled Buffer") < t.index("version 0.8")


def test_timeline_pages_a_huge_entry_without_skips(live):
    body = "20280401\n09h00\n" + "".join(f"line {i:03d} " + "z" * 60 + "\n" for i in range(60))
    live.write("current/2028.txt", body)
    live.resync()
    seen = []
    obs = live.q(budget=1500, mode="timeline", date_from="2028-04-01", date_to="2028-04-01")
    for _ in range(40):
        seen += re.findall(r"line (\d{3}) ", obs.text)
        if not obs.data["next_cursor"]:
            break
        obs = live.q(budget=1500, mode="continue", cursor=obs.data["next_cursor"])
    assert [int(x) for x in seen] == list(range(60))


# --- read -------------------------------------------------------------------------------------------


def test_read_opens_the_entry_and_continues(live):
    hit = live.q(mode="literal", query="IDEA: let the assistant")
    cite = citations(hit)[0]
    obs = live.q(mode="read", citation=cite)
    t = obs.text
    assert t.index("IDEA: let the assistant") < t.index("A user of the export tool")


def test_read_errors(live):
    assert live.q(mode="read", citation="nonsense").data["error"] == "invalid_request"
    ghost = f"diary:{uuid.uuid4()}:0-10"
    assert live.q(mode="read", citation=ghost).data["error"] == "not_found"
    real = citations(live.q(mode="literal", query="Physiotherapy"))[0]
    rev = real.split(":")[1]
    assert live.q(mode="read", citation=f"diary:{rev}:0-999999").data["error"] == "not_found"
    other = DiaryContext(room_uuid=uuid.uuid4(), agent_uuid=live.agent, models_local=True)
    assert diary_query({"mode": "read", "citation": real}, other, budget=BUDGET).data["error"] == "not_found"


def test_read_inside_multibyte_character_is_not_found(live):
    hit = citations(live.q(mode="literal", query="Rücken"))[0]
    rev, rng = hit.split(":")[1], hit.split(":")[2]
    start = int(rng.split("-")[0])
    raw = (live.root / "current" / "2027.txt").read_bytes()
    u = raw.index("ü".encode(), start)
    assert live.q(mode="read", citation=f"diary:{rev}:{u + 1}-{u + 3}").data["error"] == "not_found"


def test_citation_to_superseded_snapshot(live):
    before = citations(live.q(mode="literal", query="Slept badly"))[0]
    path = live.root / "current" / "2027.txt"
    path.write_bytes(path.read_bytes().replace(b"Slept badly", b"Slept well"))
    live.resync()
    obs = live.q(mode="read", citation=before)
    assert obs.ok and "Slept badly" in obs.text and "superseded snapshot" in obs.text


def test_citation_survives_append(live):
    before = citations(live.q(mode="literal", query="Physiotherapy"))
    path = live.root / "current" / "2027.txt"
    path.write_bytes(path.read_bytes() + b"\n20270315\n09h00\nappended\n")
    live.resync()
    after = citations(live.q(mode="literal", query="Physiotherapy"))
    assert before == after


# --- cursors -------------------------------------------------------------------------------------------


def _paged(live):
    body = "".join(f"20280501\n{h:02d}h00\npaged text {h}\n\n" for h in range(0, 24))
    live.write("current/2028.txt", body)
    live.resync()
    obs = live.q(budget=800, mode="literal", query="paged text")
    assert obs.data["next_cursor"]
    return obs.data["next_cursor"]


def test_cursor_expiry_staleness_and_binding(live):
    cursor = _paged(live)
    other = DiaryContext(room_uuid=uuid.uuid4(), agent_uuid=live.agent, models_local=True)
    assert diary_query({"mode": "continue", "cursor": cursor}, other, budget=BUDGET).data["error"] == "not_found"
    db.diary_exclude(live.src.uuid, "archive/ChangeLog.txt")   # bumps versions
    assert live.q(mode="continue", cursor=cursor).data["error"] == "cursor_stale"
    cursor2 = _paged(live)
    db.session.execute(sa.update(db.DiaryCursor).where(db.DiaryCursor.uuid == uuid.UUID(cursor2))
                       .values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    db.session.commit()
    assert live.q(mode="continue", cursor=cursor2).data["error"] == "cursor_expired"


def test_repeated_cursor_read_is_idempotent(live):
    cursor = _paged(live)
    a = live.q(budget=800, mode="continue", cursor=cursor)
    b = live.q(budget=800, mode="continue", cursor=cursor)
    assert citations(a) == citations(b)


# --- budget and telemetry ---------------------------------------------------------------------------------


@pytest.mark.parametrize("budget", [1100, 1900, 3500])
def test_rendered_text_never_exceeds_budget(live, budget):
    for args in ({"mode": "search", "query": "the"},
                 {"mode": "timeline", "date_from": "2027-03-12", "date_to": "2027-03-13"},
                 {"mode": "literal", "query": "e"}):
        obs = diary_query(args, live.ctx(), budget=budget)
        assert obs.ok and len(obs.text) <= budget
        assert_fenced(obs, budget)


def test_compact_form_carries_citations(live):
    obs = live.q(mode="search", query="physiotherapy")
    assert obs.compact and len(obs.compact) <= 400
    assert citations(obs)[0] in obs.compact and '"mode": "read"' in obs.compact


def test_telemetry_retrieved_vs_injected(live):
    obs = live.q(budget=700, mode="search", query="the")
    rows = db.session.query(db.RetrievalEvent).filter_by(
        room_uuid=live.room, target_type="diary_citation").all()
    injected = {r.target_id for r in rows if r.stage == "injected"}
    retrieved = {r.target_id for r in rows if r.stage == "retrieved"}
    assert injected == set(obs.data["injected"]) and injected < retrieved
    assert {r.source for r in rows} == {"diary.search"}


def test_budget_arithmetic_four_maximal_passages_fit():
    """4 * PASSAGE_CAP + real labels fit DIARY_OBSERVATION_CHARS (§8), and the
    budget is derived from the assistant's scratchpad."""
    from agents.assistant import AssistantAgent
    from diary.config import PASSAGE_CAP
    from diary.render import DIARY_SCRATCHPAD_RESERVE, observation_budget, render_items
    from diary.retrieval import DiaryItem, DiaryResult

    budget = observation_budget()
    assert budget == AssistantAgent.MAX_SCRATCHPAD_CHARS - DIARY_SCRATCHPAD_RESERVE == 3500
    items = [DiaryItem(
        source_uuid=uuid.UUID(int=1), source_name="a-realistic-source-name",
        snapshot_at=datetime(2027, 3, 14, 9, 12, tzinfo=UTC),
        path="current/some/deeper/folder/2027.txt", entry_uuid=uuid.uuid4(),
        passage_uuid=uuid.uuid4(), cite_revision=uuid.uuid4(), byte_start=100000 + i,
        byte_end=100700 + i, text="x" * (PASSAGE_CAP - 1) + "\n", date_local=date(2027, 3, 12),
        clock_start=None, clock_end=None, time_status="date_only", date_basis="header",
        author_token="kreese") for i in range(4)]
    result = DiaryResult(ok=True, mode="search", items=items, metadata="partial",
                         degraded_routes=["vector_unavailable"])
    rendered = render_items(result, budget)
    assert len(rendered.rendered) == 4 and len(rendered.text) <= budget


def test_item_too_large_for_budget_still_makes_progress():
    from diary.render import render_items
    from diary.retrieval import DiaryItem, DiaryResult

    items = [DiaryItem(
        source_uuid=uuid.UUID(int=1), source_name="s", snapshot_at=None, path="p.txt",
        entry_uuid=None, passage_uuid=None, cite_revision=uuid.UUID(int=2),
        byte_start=0, byte_end=700, text="y" * 700, date_local=None, clock_start=None,
        clock_end=None, time_status="unknown", date_basis="unknown", resume={"i": k})
        for k in range(2)]
    rendered = render_items(DiaryResult(ok=True, mode="timeline", items=items), 900)
    assert rendered.rendered == [] and rendered.first_unrendered == 1
    assert "too large" in rendered.text and len(rendered.text) <= 900


def test_timeline_keeps_late_night_entries_after_the_evening(live):
    live.write("current/2028.txt", "20280601\n21h00\nevening\n\n23h50\nlate\n\n00h20\nafter midnight\n")
    live.resync()
    t = live.q(mode="timeline", date_from="2028-06-01", date_to="2028-06-01").text
    assert t.index("evening") < t.index("late") < t.index("after midnight")


def test_literal_matches_inside_one_excerpt_are_not_repeated(live):
    live.write("current/2028.txt", "20280701\n09h00\nset DB_PASSWORD and APP_PASSWORD now\nthen restart\n")
    live.resync()
    obs = live.q(mode="literal", query="PASSWORD")
    assert obs.text.count("--- current/2028.txt") == 1 and "2 matches" in obs.text


def test_literal_window_snaps_to_line_boundaries(live):
    filler = "".join(f"line {i} with some words\n" for i in range(12))
    live.write("current/2028.txt", f"20280702\n09h00\n{filler}needle here\n{filler}")
    live.resync()
    obs = live.q(mode="literal", query="needle")
    block = obs.text.split("--- current/2028.txt", 1)[1].split("\n", 1)[1]
    assert block.startswith("line ") and "needle here" in block
    assert block.split("</diary_passages>")[0].rstrip("\n").endswith("words")
