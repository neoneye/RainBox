"""One detail component per event kind.

The property that makes the page scale: a kind renders without anyone having
written a special case for it, and an action renders without anyone having
written a special case for that action. Bespoke renderers are a promotion a
payload earns, never a requirement.
"""

import db
from webapp.assistant_components import event_kpis, render_event_detail


def _event(kind, label="x", *, kpis=None, payload=None, duration_ms=1000):
    return {"uuid": "u", "kind": kind, "variant": kind, "label": label,
            "start": None, "duration_ms": duration_ms, "anchor": "",
            "kpis": kpis or {}, "payload": payload or {}}


def test_every_kind_renders_without_a_bespoke_component():
    for kind in db.EVENT_KINDS:
        html = render_event_detail(_event(kind, label="the label"))
        assert html, kind
        assert "the label" in html, kind


def test_an_action_nobody_wrote_a_renderer_for_still_reads_well():
    """32 actions today and more later. A new one must cost no code."""
    html = render_event_detail(_event(
        "action", "kanban_task_teleport",
        payload={"args": {"task": "7"}, "observation": {"text": "moved"}}))

    assert "kanban_task_teleport" in html
    assert "moved" in html
    assert "task" in html


def test_python_run_shows_its_code_and_output():
    """The payload genuinely differs: a program and its output rendered as
    JSON strings in a box is worse than what the page did before."""
    html = render_event_detail(_event(
        "action", "python_run",
        payload={"args": {"code": "print(2 + 2)"},
                 "observation": {"text": "4"}}))

    assert "print(2 + 2)" in html
    assert ">4<" in html or "4" in html


def test_an_llm_pane_reports_the_call_cost():
    html = render_event_detail(_event(
        "llm", "decide → reply",
        kpis={"model": "gemma4:e4b", "input_tokens": 11100,
              "output_tokens": 68, "prefill_ms": 14000, "decode_ms": 2200,
              "cached_tokens": 8100},
        payload={"system_prompt": "sys", "user_prompt": "usr"}))

    assert "gemma4:e4b" in html
    assert "11100" in html or "11,100" in html or "11.1k" in html


def test_an_unaccounted_pane_says_nothing_measured_it():
    """It is the absence of evidence; a pane that looked like a measurement
    would be worse than an empty one."""
    html = render_event_detail(_event("unaccounted", "unaccounted"))

    assert "nothing" in html.lower() or "unmeasured" in html.lower()


def test_untrusted_payload_text_is_escaped():
    """An observation is model and tool output. It must not be able to inject
    markup into the page that renders it."""
    html = render_event_detail(_event(
        "action", "memory_query",
        payload={"args": {}, "observation": {"text": "<script>x</script>"}}))

    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_a_label_with_markup_is_escaped():
    html = render_event_detail(_event("action", "<img src=x>"))

    assert "<img" not in html


def test_kpis_are_pairs_a_renderer_can_lay_out():
    pairs = event_kpis(_event(
        "llm", "reply",
        kpis={"model": "gemma4:e4b", "input_tokens": 100,
              "output_tokens": 5, "prefill_ms": 900, "decode_ms": 100,
              "cached_tokens": None}))

    assert ("model", "gemma4:e4b") in pairs
    # A KPI with nothing recorded is omitted rather than shown as None.
    assert all(v not in (None, "None") for _, v in pairs)


def test_an_llm_pane_without_a_joined_row_still_renders():
    """Runs predating the llm_call linkage keep the KPIs the step carries."""
    html = render_event_detail(_event(
        "llm", "reply",
        kpis={"input_tokens": 100, "output_tokens": 5,
              "prefill_ms": None, "cached_tokens": None},
        payload={"system_prompt": "sys"}))

    assert html and "reply" in html


def test_the_start_pane_links_to_the_user_and_the_chat_message():
    """The run's question, with both ways back to where it came from — the
    person who asked and the message in the room."""
    html = render_event_detail(_event(
        "start", "start", duration_ms=0,
        payload={"text": "tell me about my siblings",
                 "sender_name": "Operator",
                 "sender_uuid": "2222", "message_id": 42,
                 "room_uuid": "3333", "timestamp": "2026-08-24 00:46"}))

    assert "Started by" in html
    assert '/user?id=2222' in html
    assert '/chat?id=3333&amp;msg=42' in html or '/chat?id=3333&msg=42' in html
    assert "tell me about my siblings" in html


def test_the_start_pane_without_a_room_still_names_the_asker():
    """A run seeded outside a room has no message to link to; the question and
    who asked it are still worth showing."""
    html = render_event_detail(_event(
        "start", "start", duration_ms=0,
        payload={"text": "hello", "sender_name": "Operator",
                 "sender_uuid": "2222", "message_id": None,
                 "room_uuid": "", "timestamp": ""}))

    assert "Operator" in html and "hello" in html
    assert "/chat?id=" not in html


def test_the_start_pane_escapes_the_question():
    html = render_event_detail(_event(
        "start", "start", duration_ms=0,
        payload={"text": "<script>x</script>", "sender_name": "<b>x</b>",
                 "sender_uuid": "2", "message_id": 1, "room_uuid": "3"}))

    assert "<script>" not in html
    assert "<b>x</b>" not in html
