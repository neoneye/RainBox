"""A reply's sources: named by the model, kept only when this turn showed the
link, resolved into a kind and a page, posted with the reply and shown to the
next turn (agents/reply_sources.py and the assistant loop)."""

from uuid import uuid4

import db
from agents.assistant import AssistantActionName, AssistantObservation
from agents.assistant_fakes import scripted_decisions
from agents.reply_sources import (
    DROP_DUPLICATE,
    DROP_NO_LINK,
    DROP_NOT_A_LIST,
    DROP_NOT_SEEN,
    DROP_OVER_LIMIT,
    MAX_REPLY_SOURCES,
    MAX_SOURCE_NAME_CHARS,
    check_seen,
    normalize_sources,
    reply_sources,
    resolve_source,
    sources_report,
    unused_sources,
    MAX_UNUSED_SOURCES,
)
from agents.test_assistant_actions import (  # noqa: F401 — fixtures
    _agent,
    _decision,
    _steps_for,
    app_ctx,
    room,
)

SHED_UUID = "0b1e2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def _no_match(_uuid):
    return None


# --- the argument ----------------------------------------------------------------------


def test_entries_are_read_from_dicts_and_bare_links():
    entries, dropped = normalize_sources(
        [{"name": " Garden shed ", "link": f" {SHED_UUID} "}, "https://example.org/gate"])
    assert entries == [{"name": "Garden shed", "link": SHED_UUID},
                       {"name": "", "link": "https://example.org/gate"}]
    assert dropped == []


def test_no_argument_is_no_sources():
    assert normalize_sources(None) == ([], [])


def test_a_non_list_is_dropped_whole():
    entries, dropped = normalize_sources("the shed")
    assert entries == []
    assert dropped[0]["reason"] == DROP_NOT_A_LIST


def test_empty_links_and_repeats_are_dropped_with_their_reason():
    entries, dropped = normalize_sources(
        [{"name": "Shed", "link": SHED_UUID}, {"name": "Shed again", "link": SHED_UUID},
         {"name": "Nothing", "link": ""}])
    assert [e["name"] for e in entries] == ["Shed"]
    assert [d["reason"] for d in dropped] == [DROP_DUPLICATE, DROP_NO_LINK]


def test_past_the_limit_sources_are_dropped_and_names_are_cut():
    raw = [{"name": "x" * 500, "link": f"https://example.org/{i}"}
           for i in range(MAX_REPLY_SOURCES + 2)]
    entries, dropped = normalize_sources(raw)
    assert len(entries) == MAX_REPLY_SOURCES
    assert len(entries[0]["name"]) == MAX_SOURCE_NAME_CHARS
    assert [d["reason"] for d in dropped] == [DROP_OVER_LIMIT] * 2


# --- what the turn showed ------------------------------------------------------------------


def test_a_link_is_kept_only_when_the_turn_showed_it():
    entries = [{"name": "Shed", "link": SHED_UUID},
               {"name": "Step", "link": "current_turn_steps.1.observation"}]
    kept, dropped = check_seen(entries, ["a request", f"{SHED_UUID}, garden.shed: blue"])
    assert kept == [entries[0]]
    assert dropped == [{**entries[1], "reason": DROP_NOT_SEEN}]


def _cite(n: int) -> str:
    return f"diary:{uuid4()}:{n}00-{n}99"


def test_seen_links_inside_a_copied_summary_line_are_kept_under_its_name():
    shown, unshown = _cite(1), _cite(2)
    also_shown = _cite(3)
    line = (f'search: 3 excerpts shown earlier. Reopen with {{"mode": "read", '
            f'"citation": ...}}: {shown} {unshown} {also_shown} …')
    observation = f"excerpt one [{shown}]\nexcerpt three [{also_shown}]"
    kept, dropped = check_seen([{"name": "Diary search", "link": line}], [observation])
    assert kept == [{"name": "Diary search", "link": shown},
                    {"name": "Diary search", "link": also_shown}]
    assert dropped == [{"name": "Diary search", "link": line,
                        "reason": f"{DROP_NOT_SEEN}; kept the 2 links inside it"}]


def test_a_web_address_inside_a_sentence_is_kept_without_its_punctuation():
    url = "https://example.org/page?id=7"
    kept, dropped = check_seen([{"name": "Page", "link": f"see ({url})."}], [f"fetched {url}"])
    assert kept == [{"name": "Page", "link": url}]
    assert dropped[0]["reason"] == f"{DROP_NOT_SEEN}; kept the 1 link inside it"


def test_an_unseen_citation_is_dropped_however_it_is_wrapped():
    entries = [{"name": "Old", "link": _cite(4)},
               {"name": "Old wrapped", "link": f"from an earlier answer: {_cite(5)}"}]
    kept, dropped = check_seen(entries, [f"this turn showed {_cite(6)}"])
    assert kept == []
    assert [d["reason"] for d in dropped] == [DROP_NOT_SEEN, DROP_NOT_SEEN]


def test_rescued_links_stop_at_the_limit_and_are_not_repeated():
    cites = [_cite(i) for i in range(MAX_REPLY_SOURCES + 5)]
    observation = " ".join(cites)
    entries = [{"name": "First", "link": cites[0]},
               {"name": "All", "link": "results: " + " ".join(cites)}]
    kept, _dropped = check_seen(entries, [observation])
    assert len(kept) == MAX_REPLY_SOURCES
    assert [k["link"] for k in kept] == cites[:MAX_REPLY_SOURCES]
    assert kept[0]["name"] == "First"


# --- resolving ------------------------------------------------------------------------------


def test_a_uuid_takes_its_kind_and_page_from_its_record():
    def describe(uuid):
        return {"kind": "memory claim", "uuid": uuid, "name": "garden.shed",
                "url": f"/memory?id={uuid}", "match": "exact"}
    assert resolve_source({"name": "Shed", "link": SHED_UUID}, describe) == {
        "name": "Shed", "link": SHED_UUID, "kind": "memory claim",
        "url": f"/memory?id={SHED_UUID}"}


def test_an_unnamed_uuid_takes_the_record_s_name():
    def describe(_uuid):
        return {"kind": "kanban board", "name": "Garden", "url": "/kanban?id=b"}
    assert resolve_source({"name": "", "link": SHED_UUID}, describe)["name"] == "Garden"


def test_a_uuid_with_no_page_opens_the_find_page():
    def describe(_uuid):
        return {"kind": "Q&A entry", "name": "garden.shed", "url": None}
    source = resolve_source({"name": "Shed", "link": SHED_UUID}, describe)
    assert source["kind"] == "Q&A entry"
    assert source["url"] == f"/find?q={SHED_UUID}"


def test_an_unknown_uuid_is_a_record_on_the_find_page():
    source = resolve_source({"name": "Shed", "link": SHED_UUID}, _no_match)
    assert (source["kind"], source["url"]) == ("record", f"/find?q={SHED_UUID}")


def test_a_failed_lookup_still_links_to_the_find_page():
    def describe(_uuid):
        raise RuntimeError("database gone")
    assert resolve_source({"name": "", "link": SHED_UUID}, describe)["url"] == f"/find?q={SHED_UUID}"


def test_a_web_address_is_its_own_page_and_a_path_has_none():
    web = resolve_source({"name": "Gate", "link": "https://example.org/gate"}, _no_match)
    path = resolve_source({"name": "", "link": "garden/shed.md"}, _no_match)
    assert (web["kind"], web["url"]) == ("web", "https://example.org/gate")
    assert path == {"name": "garden/shed.md", "link": "garden/shed.md",
                    "kind": "path", "url": None}


def test_the_report_lists_the_kept_then_the_dropped():
    sources, dropped = reply_sources(
        [{"name": "Shed", "link": SHED_UUID}, {"name": "Pond", "link": "pond.md"}],
        [SHED_UUID], _no_match)
    report = sources_report(sources, dropped)
    assert report.splitlines() == [
        "sources: 1",
        f"1. Shed — {SHED_UUID} (record)",
        "dropped: 1",
        f"- Pond — pond.md: {DROP_NOT_SEEN}",
    ]


POND = "pond-notes.md"


def test_unused_sources_are_the_shown_items_the_reply_did_not_cite():
    shown = [{"link": SHED_UUID, "name": "shed"},
             {"link": POND, "name": "Pond notes", "kind": "diary passage"},
             {"link": POND, "name": "again"},          # shown twice, listed once
             {"link": "", "name": "no link"}]
    sources, _ = reply_sources([{"name": "Shed", "link": SHED_UUID}], [SHED_UUID],
                               _no_match, shown=shown)
    assert unused_sources(shown, sources, _no_match) == [
        {"name": "Pond notes", "link": POND, "kind": "diary passage", "url": None}]


def test_a_cited_shown_item_lends_the_source_its_kind_page_and_name():
    shown = [{"link": SHED_UUID, "name": "Garden shed", "kind": "kanban task",
              "url": "/kanban?id=x"}]
    sources, _ = reply_sources([SHED_UUID], [SHED_UUID], _no_match, shown=shown)
    assert sources == [{"name": "Garden shed", "link": SHED_UUID,
                        "kind": "kanban task", "url": "/kanban?id=x"}]
    named, _ = reply_sources([{"name": "Shed", "link": SHED_UUID}], [SHED_UUID],
                             _no_match, shown=shown)
    assert named[0]["name"] == "Shed"                   # the model's name wins


def test_unused_sources_are_capped():
    shown = [{"link": f"note-{i}.md"} for i in range(MAX_UNUSED_SOURCES + 5)]
    unused = unused_sources(shown, [], _no_match)
    assert len(unused) == MAX_UNUSED_SOURCES
    assert unused[0] == {"name": "note-0.md", "link": "note-0.md", "kind": "path", "url": None}


def test_the_report_ends_with_the_unused():
    report = sources_report([], [], [{"name": "Pond notes", "link": POND,
                                      "kind": "diary passage", "url": None}])
    assert report.splitlines() == [
        "sources: 0", "unused: 1", f"- Pond notes — {POND} (diary passage)"]


# --- the loop --------------------------------------------------------------------------------


def _reply_with_sources(room, sources, shown_items=None):
    """One read whose observation shows SHED_UUID, then a reply citing `sources`."""
    room_uuid, message_uuid = room
    agent = _agent()
    agent._dispatch_action = lambda ctx, decision: AssistantObservation(
        ok=True, text=f"{SHED_UUID}, garden.shed: the shed is blue",
        data={"shown_items": shown_items} if shown_items else None)
    agent._decide_next_step = scripted_decisions(
        _decision(AssistantActionName.FIND_UUID, query="0b1e2c3d"),
        _decision(AssistantActionName.REPLY, message="The shed is blue.",
                  sources=sources),
    )
    result = agent.handle(
        uuid4(), {"room_uuid": str(room_uuid), "message_uuid": str(message_uuid)})
    assert result["status"] == "finished"
    reply = db.list_room_messages(room_uuid)[-1]
    step = [s for s in _steps_for(result["assistant_run_uuid"]) if s.action == "reply"][-1]
    return reply, step


def test_the_reply_posts_the_sources_its_turn_showed(room):
    reply, step = _reply_with_sources(room, [
        {"name": "Garden shed", "link": SHED_UUID},
        {"name": "Invented", "link": "1111aaaa-2222-4333-8444-555566667777"},
    ])
    assert reply["text"] == "The shed is blue."
    assert reply["meta"]["sources"] == [{
        "name": "Garden shed", "link": SHED_UUID, "kind": "record",
        "url": f"/find?q={SHED_UUID}"}]
    # The drop is reported on the reply's own row, where it happened.
    assert step.observation is not None
    data = step.observation["data"]
    assert [s["link"] for s in data["sources"]] == [SHED_UUID]
    assert data["dropped_sources"][0]["reason"] == DROP_NOT_SEEN
    assert "dropped: 1" in step.observation["text"]


def test_the_reply_keeps_what_its_reads_showed_and_it_did_not_cite(room):
    reply, step = _reply_with_sources(
        room, [{"name": "Garden shed", "link": SHED_UUID}],
        shown_items=[{"link": SHED_UUID, "name": "garden.shed"},
                     {"link": POND, "name": "Pond notes", "kind": "diary passage"}])
    assert [s["link"] for s in reply["meta"]["sources"]] == [SHED_UUID]
    assert reply["meta"]["unused_sources"] == [
        {"name": "Pond notes", "link": POND, "kind": "diary passage", "url": None}]
    assert step.observation["data"]["unused_sources"] == reply["meta"]["unused_sources"]
    assert "unused: 1" in step.observation["text"]


def test_a_reply_citing_nothing_still_keeps_what_was_checked(room):
    reply, _ = _reply_with_sources(room, None, shown_items=[{"link": POND, "name": "Pond"}])
    assert "sources" not in reply["meta"]
    assert [u["link"] for u in reply["meta"]["unused_sources"]] == [POND]


def test_a_reply_without_sources_posts_none(room):
    reply, step = _reply_with_sources(room, None)
    assert "sources" not in reply["meta"] and "unused_sources" not in reply["meta"]
    assert step.observation is None


# --- the next turn ----------------------------------------------------------------------


def test_an_earlier_reply_s_sources_follow_it_in_the_history():
    agent = _agent()
    messages = [
        {"sender_type": "human", "text": "what colour is the shed?"},
        {"sender_type": "agent", "text": "The shed is blue.",
         "meta": {"sources": [{"name": "Garden shed", "link": SHED_UUID,
                               "kind": "memory claim", "url": "/memory?id=x"}]}},
        {"sender_type": "human", "text": "and the door?"},
    ]
    prompt = agent._build_user_prompt(messages=messages, scratchpad=[], step_index=0)
    history = prompt.split("<conversation_history_xml>")[1].split("</conversation_history_xml>")[0]
    assert (f'<message_sources>\n<source kind="memory claim" link="{SHED_UUID}">'
            "Garden shed</source>") in history.replace("  ", "")
    # After the reply it belongs to, not inside its text.
    assert history.index("The shed is blue.") < history.index("<message_sources>")
    assert "Garden shed" not in history.split("<message_sources>")[0]


def test_the_turn_instructions_say_sources_are_places_to_look():
    from agents.assistant import DECIDE_TURN_INSTRUCTIONS
    text = " ".join(DECIDE_TURN_INSTRUCTIONS.split())
    assert "A message_sources section after one of your earlier answers" in text
    assert "They are places to look, not facts" in text
