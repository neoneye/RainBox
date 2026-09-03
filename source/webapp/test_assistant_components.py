"""One detail component per event kind.

The property that makes the page scale: a kind renders without anyone having
written a special case for it, and an action renders without anyone having
written a special case for that action. Bespoke renderers are a promotion a
payload earns, never a requirement.
"""

import json

import db
from webapp.assistant_components import (
    event_kpis, event_markdown, render_event_detail)


def _event(kind, label="x", *, kpis=None, payload=None, duration_ms=1000):
    return {"uuid": "u", "kind": kind, "variant": kind, "label": label,
            "start": None, "duration_ms": duration_ms, "anchor": "",
            "kpis": kpis or {}, "payload": payload or {}}


def _review_event(**kw):
    """The gate's row as the read model builds it: an `llm` event whose
    variant is what selects the reviewer's renderer."""
    return dict(_event("llm", "second opinion", **kw), variant="review")


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


def _recall_filter_pane(**found):
    return render_event_detail(_event(
        "activity", "memory_query \u203a recall filter",
        payload={"found": found}))


_RERANKED = dict(
    mode="reranker", scorer_model="mmarco-mMiniLMv2-L12-H384-v1",
    service_ms=203, max_length=512, query="where I live",
    candidates=[
        {"qa_id": "qa-1", "path": "person.location", "kept": True,
         "rerank_score": 0.9783, "tokens": 25,
         "document": "where do you live?\nIn a house."},
        {"qa_id": "qa-2", "path": "system.uptime_host", "kept": False,
         "rerank_score": 0.005, "tokens": 66,
         "document": "how long has the host been up?\n2 days."},
    ])


def test_the_recall_filter_pane_leads_with_the_scorer_and_the_message():
    """The candidate list is the long part, and under sorted JSON it buried
    the two things the reader opens the pane for: what scored, and what it was
    asked. The reranker backend has no call row to carry either."""
    html = _recall_filter_pane(**_RERANKED)

    assert "mmarco-mMiniLMv2-L12-H384-v1" in html
    assert "203 ms in the service" in html
    assert "512 tokens" in html
    assert "where I live" in html                 # the message scored against
    assert "In a house." in html                  # the document it was paired with
    assert "sent to the scorer (2)" in html
    # The scorer and the message read before the list, not after it.
    assert html.index("mmarco") < html.index("qa-1")
    assert html.index("where I live") < html.index("qa-1")


def test_the_recall_filter_pane_shows_the_answer_after_the_input():
    """Input and output were one merged row per candidate, so a score sat
    beside a token count with nothing saying which of them the scorer
    produced — and no way to read what came back at all."""
    html = _recall_filter_pane(**_RERANKED)

    assert "what it answered (1 of 2 kept)" in html
    assert "0.9783" in html and "kept" in html
    assert "0.0050" in html and "dropped" in html
    # The answer reads AFTER the input it was given, and the input block no
    # longer carries the scores.
    assert html.index("sent to the scorer") < html.index("what it answered")
    sent = html[html.index("sent to the scorer"):html.index("what it answered")]
    assert "0.9783" not in sent and "kept" not in sent


def test_the_recall_filter_pane_answers_in_the_llm_backends_own_terms():
    """One number is the reranker's answer; three scales are the LLM's. A pane
    that printed one shape for both would be inventing the other."""
    html = _recall_filter_pane(
        mode="llm", scorer_model="gemma4:e4b", group_from="assistant.default",
        candidates=[{"qa_id": "qa-1", "path": "person.location", "kept": True,
                     "direct": 5, "indirect": 3, "relevancy": 4,
                     "matched_question": "where do you live?"}])

    assert "direct 5 · indirect 3 · relevancy 4" in html
    assert "what it answered (1 of 1 kept)" in html


def test_the_recall_filter_pane_shows_the_llm_backends_note():
    """Deliberately kept out of the assistant's own observation, so this pane
    is where the operator reads it."""
    html = _recall_filter_pane(
        mode="llm", scorer_model="gemma4:e4b", group_from="assistant.default",
        reasoning="nothing here answers the question", candidates=[])

    assert "assistant.default model group" in html
    assert "nothing here answers the question" in html


def test_a_gated_recall_filter_pane_says_nothing_was_scored():
    """An empty candidate list here is not "everything was irrelevant" — the
    filter never ran."""
    html = _recall_filter_pane(mode="gated", reason="filter_failed")

    assert "did not run" in html
    assert "filter_failed" in html


def test_an_activity_pane_with_no_findings_still_says_what_it_is():
    html = render_event_detail(_event("activity", "python_run \u203a execute"))

    assert "own work" in html


def test_an_activity_s_findings_are_escaped():
    html = render_event_detail(_event(
        "activity", "x", payload={"found": {"path": "<script>x</script>"}}))

    assert "<script>" not in html


def _intent(**over):
    row = {"uuid": "1111", "capability_name": "memory_remember",
           "state": "proposed", "preview_text": "remember x",
           "payload": {"text": "x"}, "result": {}}
    row.update(over)
    return row


def test_a_proposed_write_offers_the_decision_it_is_waiting_on():
    """The operator's only way to approve a pending write. It has to travel
    with the action that proposed it or the run stalls with nothing to press.
    """
    html = render_event_detail(_event(
        "action", "memory_remember", kpis={"status": "ok"},
        payload={"args": {}, "intents": [_intent()]}))

    assert "/write-intents/1111/confirm" in html
    assert "/write-intents/1111/reject" in html
    assert "remember x" in html


def test_a_completed_write_offers_undo_only_where_it_can_be_undone():
    html = render_event_detail(_event(
        "action", "memory_remember", kpis={"status": "ok"},
        payload={"args": {}, "intents": [
            _intent(state="completed", result={"undo": {"kind": "x"}})]}))

    assert "/write-intents/1111/undo" in html
    assert "confirm" not in html

    bare = render_event_detail(_event(
        "action", "memory_remember", kpis={"status": "ok"},
        payload={"args": {}, "intents": [_intent(state="completed")]}))

    assert "/undo" not in bare


def test_a_write_already_undone_offers_nothing():
    html = render_event_detail(_event(
        "action", "memory_remember", kpis={"status": "ok"},
        payload={"args": {}, "intents": [_intent(state="undone")]}))

    assert "/confirm" not in html and "/reject" not in html
    assert "/undo" not in html
    assert "undone" in html


def test_a_write_intent_s_text_is_escaped():
    """A preview is model output and a capability name can arrive as data."""
    html = render_event_detail(_event(
        "action", "x", payload={"args": {}, "intents": [
            _intent(preview_text="<script>x</script>",
                    capability_name="<img src=q>")]}))

    assert "<script>" not in html and "<img" not in html


def test_the_run_s_opening_shows_a_write_that_belongs_to_no_step():
    html = render_event_detail(_event(
        "start", "start", duration_ms=0,
        payload={"text": "hi", "sender_name": "Operator", "sender_uuid": "2",
                 "message_id": 1, "room_uuid": "3",
                 "intents": [_intent()]}))

    assert "/write-intents/1111/confirm" in html


def test_a_write_block_is_not_wrapped_in_a_pre():
    """It holds buttons, not text as it was sent. Inside a <pre> they render
    monospace and the long-block clamp tries to fold them away."""
    html = render_event_detail(_event(
        "action", "memory_remember", kpis={"status": "ok"},
        payload={"args": {}, "intents": [_intent()]}))

    after = html[html.index("<h5>writes"):]
    after = after[after.index("</h5>") + len("</h5>"):]
    assert after.startswith('<div class="intent')


def test_a_pane_with_no_writes_shows_no_write_block():
    html = render_event_detail(_event(
        "action", "memory_query", kpis={"status": "ok"},
        payload={"args": {}, "observation": {"text": "f"}}))

    assert "write-intents" not in html


def test_a_skipped_pane_says_the_call_was_never_made():
    """Not a failure and not a success — nothing ran. A pane that looked like
    either would be worse than one that says so."""
    html = render_event_detail(_event(
        "skipped", "recall_filter", duration_ms=None,
        payload={"reason": "no model group bound"}))

    assert "never made" in html or "not made" in html
    assert "no model group bound" in html


def test_the_gate_says_what_it_is_for():
    """It reads "second opinion" on a row between a decision and the action it
    gated; without a description that is a name and no explanation."""
    html = render_event_detail(_review_event(kpis={"verdict": "approved"}))

    assert "before it is allowed to run" in html


def test_a_review_pane_leads_with_its_verdict():
    """The one thing a review is read for. It gated an action that then ran or
    did not, so it reads as an outcome rather than as a figure on the meta
    line beside the token counts."""
    html = render_event_detail(_review_event(
        kpis={"verdict": "rejected"},
        payload={"problems": [{"category": "safety", "text": "writes a file"}],
                 "model_response": "{}"}))

    assert "<h5>verdict<" in html
    assert "rejected" in html
    assert "writes a file" in html
    # The finding, not its tag: a category beside the sentence puts a label
    # where the reader is looking for what was actually wrong.
    assert "safety" not in html


def test_a_review_that_never_ran_shows_the_reason_not_a_verdict():
    html = render_event_detail(_review_event(
        kpis={"verdict": "skipped"},
        payload={"skip_reason": "no model group bound", "problems": []}))

    assert "no model group bound" in html


def test_a_review_with_no_problems_shows_no_empty_block():
    html = render_event_detail(_review_event(
        kpis={"verdict": "approved"},
        payload={"problems": [], "model_response": "{}"}))

    assert "problems" not in html


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


def test_a_memory_query_pane_does_not_reprint_the_observation_above_itself():
    """It printed the whole observation as an expanded block of its own, and
    the pane ends with that same payload — the recall filter's share of it was
    seventeen kilobytes of documents, twice."""
    html = render_event_detail(_event(
        "action", "memory_query", kpis={"status": "ok"},
        payload={"args": {"query": "where I live"},
                 "observation": {"text": "facts", "data": {"qa_static": 1}}}))

    assert html.count("qa_static") == 1
    assert "retrieval" not in html


def test_an_action_s_status_is_a_block_not_a_meta_field():
    """The meta line reports what a call cost — tokens, timings, a clock. How
    the action ENDED is an outcome, so it reads as one of the labelled blocks
    the pane already uses for what was asked and what came back.
    """
    html = render_event_detail(_event(
        "action", "memory_query", kpis={"status": "ok"},
        payload={"args": {}, "observation": {"text": "facts"}}))

    assert '<h5>status<' in html
    assert 'title="How the action ended"' not in html
    assert '<span class="ev-kpi" title="How the action ended">' not in html


def test_every_action_renderer_shows_what_the_action_returned():
    """A bespoke renderer must not be the one that drops the structured half
    of a result. python_run has one, and its result data is where the run
    records how long the program took."""
    for label in ("python_run", "memory_query", "kanban_task_teleport"):
        html = render_event_detail(_event(
            "action", label, kpis={"status": "ok"},
            payload={"args": {"code": "x"},
                     "observation": {"text": "y",
                                     "data": {"duration_seconds": 0.01}}}))
        assert "duration_seconds" in html, label


def test_every_action_renderer_shows_the_status():
    """Bespoke renderers must not be the ones that quietly drop it."""
    for label, payload in (
        ("python_run", {"args": {"code": "x"}, "observation": {"text": "y"}}),
        ("memory_query", {"args": {"query": "q"}, "observation": {"text": "r"}}),
        ("kanban_task_teleport", {"args": {}, "observation": {"text": "ok"}}),
    ):
        html = render_event_detail(_event(
            "action", label, kpis={"status": "error"}, payload=payload))
        assert "<h5>status<" in html, label
        assert ">error<" in html, label


def test_a_call_has_no_status_block():
    """Only an action ends in a way worth naming; a model call reports its
    cost and its answer."""
    html = render_event_detail(_event(
        "llm", "reply", payload={"model_response": "{}"}))

    assert "<h5>status<" not in html


def test_a_structured_answer_offers_the_reading_it_needs():
    """A structured call's answer arrives as one long line of JSON — the exact
    bytes the provider sent, and unreadable. The block offers to indent it,
    and ships the raw text so the reader opts in rather than being handed a
    reformatting they have to trust."""
    html = render_event_detail(_event("llm", "decide → reply", payload={
        "model_response": '{"reason":"enough evidence","action":"reply"}'}))

    assert '<pre class="ev-pre" data-json>' in html
    assert 'data-view="raw"' in html and 'data-view="pretty"' in html
    # The raw bytes, not an indented copy of them.
    assert '{&#34;reason&#34;:&#34;enough evidence&#34;,&#34;action&#34;:&#34;reply&#34;}' in html


def test_prose_is_not_offered_a_reading_it_does_not_have():
    """Most of what a pane holds is text. A switch on a block that cannot
    change says there is something to see there."""
    html = render_event_detail(_event("llm", "decide → reply", payload={
        "model_response": "I could not complete that."}))

    assert "ev-view" not in html
    assert "data-json" not in html


def test_a_scalar_is_json_but_has_no_second_reading():
    """`"ok"` and `12` are valid JSON documents and read the same either way."""
    for scalar in ('"ok"', "12", "true", "null"):
        html = render_event_detail(_event("llm", "x", payload={
            "model_response": scalar}))
        assert "ev-view" not in html, scalar


def test_a_payload_that_was_never_text_gets_no_switch():
    """An action's arguments are a dict — serialized for display, indented
    already. There is no raw form to switch back to, so offering one would
    promise a reading that does not exist."""
    html = render_event_detail(_event("action", "kanban_create", payload={
        "args": {"title": "the report", "column": "doing"}}))

    assert "ev-view" not in html
    # And it is shown indented, as it always was.
    assert "&#34;title&#34;: &#34;the report&#34;" in html


def test_a_collapsed_json_block_keeps_its_switch_inside_the_summary():
    """The prompts are collapsed, and a structured call's user prompt can be
    JSON too. The switch rides in the summary so it is reachable without
    opening the block first."""
    html = render_event_detail(_event("llm", "x", payload={
        "user_prompt": '{"request":"how far is the moon"}'}))

    summary = html.split("<summary>")[1].split("</summary>")[0]
    assert "ev-view" in summary


def test_every_block_can_be_copied():
    """A prompt is read somewhere else as often as it is read here, and
    selecting twelve thousand characters by dragging is its own ordeal."""
    html = render_event_detail(_event("llm", "decide → reply", payload={
        "system_prompt": "you are the assistant",
        "user_prompt": "how far is the moon",
        "model_response": "About 384400 km.",
        "log": [{"label": "profile", "text": "default"}]}))

    # Exactly one per block — the collapsed prompts included, so a prompt can
    # be copied without opening it first.
    assert html.count("ev-block") == 4
    assert html.count('class="ev-copy"') == 4
    # And each sits in its own block's label, ahead of the text it copies.
    for block in html.split("ev-block")[1:]:
        block = block.split("</pre>")[0]
        assert "ev-copy" in block, block[:80]


def test_a_note_is_not_something_to_copy():
    """The prose a pane writes when there is nothing recorded is the page
    talking, not a record. A copy button on it offers the page's own words as
    though they came from the run."""
    html = render_event_detail(_event("unaccounted", "unaccounted"))

    assert "ev-note" in html
    assert "ev-copy" not in html


def test_the_controls_ride_with_the_block_s_own_label():
    """Not aligned to the pane edge: they belong to that block, and a control
    at the edge reads as belonging to the pane."""
    html = render_event_detail(_event("llm", "x", payload={
        "model_response": '{"action":"reply"}'}))

    acts = html.split('<h5>response')[1].split("</h5>")[0]
    assert "ev-view" in acts and "ev-copy" in acts
    # The switch first, then copy: one changes how you are looking, the other
    # acts on what you are looking at.
    assert acts.index("ev-view") < acts.index("ev-copy")


# --- the cache view's boundaries ----------------------------------------------


def _cache_attr(html: str, title: str) -> dict | None:
    """The `data-cache` payload on the pane titled `title`, or None."""
    import html as html_mod
    import re
    block = html.split(f"<summary>{title} (")[1].split("</details>")[0]
    m = re.search(r'data-cache="([^"]*)"', block)
    return json.loads(html_mod.unescape(m.group(1))) if m else None


def _cached_llm(system: str, user: str, **kpis) -> dict:
    return _event("llm", "decide → reply",
                  kpis={"input_tokens": 100, **kpis},
                  payload={"system_prompt": system, "user_prompt": user})


def test_a_prefix_that_ends_inside_the_system_prompt_leaves_the_user_prompt_cold():
    """Prefix counts are lengths over the flattened prompt, system first. A
    cache that stopped partway through the system prompt therefore covers
    part of that pane and none of the next."""
    system, user = "s" * 40, "u" * 40
    # Flattened: "<system>" + 40 + "\n<user>" + 40 = 95 chars. 20 tokens of
    # 100 is 19 chars: 8 of tag, 11 of system prompt.
    html = render_event_detail(_cached_llm(
        system, user, cached_tokens=20, reusable_tokens=20))

    assert _cache_attr(html, "system prompt")["cached"] == 11
    assert _cache_attr(html, "user prompt")["cached"] == 0


def test_a_prefix_past_the_system_prompt_fills_it_and_reaches_into_the_user_prompt():
    system, user = "s" * 40, "u" * 40
    # 60 tokens of 100 over 95 chars is 57 chars: the tag (8), the system
    # prompt (40), the newline and user tag (7), then 2 of the user prompt.
    html = render_event_detail(_cached_llm(
        system, user, cached_tokens=60, reusable_tokens=100))

    sys_attr = _cache_attr(html, "system prompt")
    user_attr = _cache_attr(html, "user prompt")
    assert sys_attr["cached"] == 40 and sys_attr["reusable"] == 40
    assert user_attr["cached"] == 2 and user_attr["reusable"] == 40
    # The counts ride along so the page can name them in tokens.
    assert user_attr["cached_tokens"] == 60
    assert user_attr["reusable_tokens"] == 100
    assert user_attr["prompt_tokens"] == 100


def test_a_calibrating_call_still_places_what_is_exact():
    """No cached estimate yet, but the reusable prefix owes nothing to timing:
    the amber band can be drawn and the green one honestly left out."""
    html = render_event_detail(_cached_llm(
        "s" * 40, "u" * 40, cached_tokens=None, reusable_tokens=100))

    attr = _cache_attr(html, "user prompt")
    assert attr["cached"] is None
    assert attr["reusable"] == 40


def test_a_call_with_no_prefix_counts_carries_no_cache_attribute():
    html = render_event_detail(_cached_llm("s" * 40, "u" * 40))

    assert "data-cache" not in html


def test_the_cache_attribute_stays_off_the_markdown():
    """The export reads blocks, not attributes: what a call sent is what the
    document quotes, with nothing about how the page colours it."""
    lines = event_markdown(_cached_llm(
        "s" * 40, "u" * 40, cached_tokens=60, reusable_tokens=100))

    assert not any("data-cache" in line or "reusable" in line for line in lines)
