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


def test_an_activity_pane_shows_what_the_phase_found():
    """The reason the pane exists. "Nothing finer was recorded inside it" was
    a dead end printed over a payload that was sitting right there."""
    html = render_event_detail(_event(
        "activity", "memory_query \u203a recall filter",
        payload={"found": {"mode": "llm", "candidates": [
            {"path": "human.x.location", "kept": True, "score": 1000}]}}))

    assert "human.x.location" in html
    assert "Nothing finer" not in html


def test_an_activity_pane_with_no_findings_still_says_what_it_is():
    html = render_event_detail(_event("activity", "python_run \u203a execute"))

    assert "own work" in html


def test_an_activity_s_findings_are_escaped():
    html = render_event_detail(_event(
        "activity", "x", payload={"found": {"path": "<script>x</script>"}}))

    assert "<script>" not in html


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


def test_kpis_are_fields_a_renderer_can_lay_out():
    fields = event_kpis(_event(
        "llm", "reply",
        kpis={"model": "gemma4:e4b", "input_tokens": 100,
              "output_tokens": 5, "prefill_ms": 900, "decode_ms": 100,
              "cached_tokens": None}))
    by_label = {f["label"]: f["text"] for f in fields}

    assert by_label["model"] == "gemma4:e4b"
    # A KPI with nothing recorded is omitted rather than shown as None.
    assert all(f["text"] not in (None, "None") for f in fields)
    # Every field carries the hover that says what the number means.
    assert all(f["title"] for f in fields)


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


def test_a_call_pane_always_says_what_the_call_was_for():
    """A pane with no description leaves the reader to know from the name
    alone what `response_language_classifier` does."""
    html = render_event_detail(_event("llm", "response_language_classifier"))

    assert "determine which language(s) the reply should use" in html


def test_a_decide_call_is_described_by_the_action_it_chose():
    html = render_event_detail(_event("llm", "decide → memory_query"))

    assert "recall facts" in html


def test_a_code_driven_call_gets_its_own_description():
    """acceptance_criteria is both a capability and a loop-issued call, and
    the two do different things."""
    html = render_event_detail(_event("llm", "acceptance_criteria"))

    assert "establish what a good reply must satisfy" in html


def test_an_action_pane_is_described_too():
    html = render_event_detail(_event(
        "action", "python_run",
        payload={"args": {"code": "x"}, "observation": {"text": "y"}}))

    assert "python" in html.lower()


def test_the_prompts_are_collapsed_and_toggleable():
    """A 50k-token prompt open by default buries every KPI above it."""
    html = render_event_detail(_event(
        "llm", "reply",
        payload={"system_prompt": "S" * 40, "user_prompt": "U" * 60,
                 "model_response": "{}"}))

    assert "<details" in html
    assert "system prompt (40 chars)" in html
    assert "user prompt (60 chars)" in html
    # Collapsed: a <details> without `open`.
    assert "<details open" not in html
    # And addressable, so the live refresh can reopen what was open.
    assert "data-k=" in html


def test_the_log_reads_before_the_prompts():
    """The log is what the call was working from — the profile in force, the
    switch states — so it frames everything below it. Put after the prompts it
    sat past tens of thousands of characters, which is where the step sections
    do NOT put it.
    """
    html = render_event_detail(_event(
        "llm", "reply",
        payload={"system_prompt": "S", "user_prompt": "U",
                 "model_response": "{}", "log": {"profile": "x"}}))

    assert html.index("log (") < html.index("system prompt (")
    assert html.index("system prompt (") < html.index("user prompt (")


def test_an_llm_pane_shows_the_model_link_and_throughput():
    html = render_event_detail(_event(
        "llm", "reply",
        kpis={"model": "gemma4:e4b",
              "model_uuid": "9999", "input_tokens": 6180,
              "output_tokens": 1024},
        duration_ms=14600))

    assert '/model?id=9999' in html
    assert "model ↗" in html
    assert "in 6180" in html and "out 1024" in html
    assert "tok/s" in html
    assert "took 14.6s" in html


def test_a_call_bound_by_a_group_links_to_the_group(event=None):
    """Some calls record the group they resolved rather than the config they
    landed on — which model answers is settled by the binding, and that is the
    page a reader wants."""
    html = render_event_detail(_event(
        "llm", "run_summarizer",
        kpis={"model": "gemma4:e4b",
              "model_group_uuid": "a5941783", "model_uuid": None}))

    assert "/modelgroup?id=a5941783" in html
    assert "model \u2197" in html
    assert "/model?id=" not in html


def test_the_config_link_still_wins_where_one_was_recorded():
    """A step records the exact override it ran on. That is finer than the
    group and stays the link."""
    html = render_event_detail(_event(
        "llm", "reply",
        kpis={"model": "gemma4:e4b", "model_uuid": "9999",
              "model_group_uuid": "a5941783"}))

    assert "/model?id=9999" in html
    assert "/modelgroup" not in html


def test_a_long_prompt_is_shown_whole():
    """The inspector exists to inspect prompts. Truncating one is the single
    thing it must not do — and the prompt-cache work turns on reading the exact
    bytes a call sent, where a clipped tail hides the divergence being hunted.

    Nothing is paid for it either: the block is collapsed until opened, so a
    long prompt costs no paint, and the page already carries the same text in
    the step section below.
    """
    prompt = "".join(f"line {i}\n" for i in range(9000))
    assert len(prompt) > 40_000

    html = render_event_detail(_event(
        "llm", "decide → reply", payload={"user_prompt": prompt}))

    assert f"user prompt ({len(prompt)} chars)" in html
    assert "line 8999" in html
    assert "first 40000 characters" not in html


def test_a_pane_carries_its_identity_for_the_header_to_show():
    """The header names what is being inspected, so the pane exposes its label
    and description rather than printing them again above the content."""
    from webapp.assistant_components import event_description

    html = render_event_detail(_event("llm", "recall_filter"))

    assert 'data-label="recall_filter"' in html
    assert 'data-desc="score what memory_query recalled for relevance"' in html
    # And which step it belongs to, which the header shows beside the name.
    assert 'data-step=""' in html
    assert 'data-step="Step 3 start"' in render_event_detail(
        dict(_event("llm", "recall_filter"), step_ref="Step 3 start"))
    # Not repeated inside the pane: the header is where identity lives now.
    assert "<h4>" not in html
    assert "ev-caption" not in html

    assert event_description({"kind": "llm", "label": "recall_filter"}) == (
        "score what memory_query recalled for relevance")


def test_a_kind_with_no_action_description_falls_back_to_what_it_is():
    """Every event says something in the header; an unaccounted stretch has no
    catalog entry but is not nameless."""
    from webapp.assistant_components import event_description

    assert event_description({"kind": "unaccounted", "label": "unaccounted"})
    assert event_description({"kind": "start", "label": "start"}) == (
        "the request that began the run")


def test_the_pane_still_carries_its_kpis_and_body():
    html = render_event_detail(_event(
        "llm", "reply", kpis={"input_tokens": 10, "output_tokens": 2},
        payload={"model_response": "{}"}, duration_ms=1000))

    assert "in 10" in html and "took 1.0s" in html
    assert "response" in html


def test_every_block_renders_in_one_typeface():
    """A prose block and a payload block read as the same kind of thing: text
    exactly as it was sent or returned. Splitting them into two typefaces made
    the request under a start event sans-serif while the same message rendered
    monospace in the trigger card a few hundred pixels below.
    """
    start = render_event_detail(_event(
        "start", "start", duration_ms=0,
        payload={"text": "tell me where I live", "sender_name": "Operator",
                 "sender_uuid": "2", "message_id": 1, "room_uuid": "3"}))
    call = render_event_detail(_event(
        "llm", "reply", payload={"model_response": "{}"}))

    for html in (start, call):
        assert "ev-text" not in html
        assert 'class="ev-pre"' in html


def test_a_memory_query_pane_reports_what_it_found():
    """The counts the step's table shows, on the event's own meta line."""
    html = render_event_detail(_event(
        "action", "memory_query",
        kpis={"status": "ok", "qa_static": 3, "qa_dynamic": 0,
              "memory": 0, "truncated": 1, "omitted": 0},
        payload={"args": {}, "observation": {"text": "facts"}}))

    for shown in ("QA static 3", "QA dynamic 0", "memory 0",
                  "truncated 1", "omitted 0"):
        assert shown in html, shown
    # And each says what it means, as the table's headers do.
    assert "number of QA static items" in html


def test_an_action_s_status_is_a_block_not_a_meta_field():
    """The meta line reports what a call cost — tokens, timings, a clock. How
    the action ENDED is an outcome, so it reads as one of the labelled blocks
    the pane already uses for what was asked and what came back.
    """
    html = render_event_detail(_event(
        "action", "memory_query", kpis={"status": "ok"},
        payload={"args": {}, "observation": {"text": "facts"}}))

    assert '<h5>status</h5>' in html
    assert 'title="How the action ended"' not in html
    assert '<span class="ev-kpi" title="How the action ended">' not in html


def test_every_action_renderer_shows_the_status():
    """Bespoke renderers must not be the ones that quietly drop it."""
    for label, payload in (
        ("python_run", {"args": {"code": "x"}, "observation": {"text": "y"}}),
        ("memory_query", {"args": {"query": "q"}, "observation": {"text": "r"}}),
        ("kanban_task_teleport", {"args": {}, "observation": {"text": "ok"}}),
    ):
        html = render_event_detail(_event(
            "action", label, kpis={"status": "error"}, payload=payload))
        assert "<h5>status</h5>" in html, label
        assert ">error<" in html, label


def test_a_call_has_no_status_block():
    """Only an action ends in a way worth naming; a model call reports its
    cost and its answer."""
    html = render_event_detail(_event(
        "llm", "reply", payload={"model_response": "{}"}))

    assert "<h5>status</h5>" not in html
