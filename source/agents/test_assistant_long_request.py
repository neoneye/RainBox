"""Tests for the long-request path: a request too big for the prompt travels
with its middle dropped, tagged so every reader knows the seam is a cut, and a
code-driven summary call before step 0 describes what was dropped as
<current_user_request_summary_markdown> alongside the shortened request.

Deterministic: the summary live-model seam (`_summarize_request`) is stubbed,
so the ordering, gating, prompt shape, and trace properties are exercised
without a model.
"""

from uuid import uuid4

from agents.assistant import (
    ASSISTANT_SYSTEM_PROMPT,
    AssistantAgent,
    REPLY_AUDIT_SYSTEM_PROMPT,
    REQUEST_SUMMARY_SYSTEM_PROMPT,
    RequestSummary,
    SECOND_OPINION_SYSTEM_PROMPT,
    TRUNCATED_REQUEST_SECTION,
)


def _agent() -> AssistantAgent:
    return AssistantAgent(agent_uuid=uuid4(), name="assistant",
                          send=lambda _: None)


def _paste(size: int) -> str:
    """A request whose two ends differ, so a middle cut is visible in both."""
    body = "".join(f"line {i}\n" for i in range(size))
    return f"OPENING QUESTION\n{body}\nCLOSING LINE"


def _summary() -> RequestSummary:
    return RequestSummary(
        content_type="a build log",
        summary="The user pasted a build log and asks why it failed.",
        key_details=["exit status 2"],
    )


# --- the cut itself -------------------------------------------------------


def test_short_text_is_returned_unchanged():
    assert AssistantAgent._truncate_middle("hello", 8000) == "hello"


def test_truncate_middle_keeps_both_ends():
    """A head-only cut throws away whichever end the operator put last: a
    pasted log closes with the failure, and a request closes with the material
    it is about."""
    text = "A" * 100 + "B" * 100
    cut = AssistantAgent._truncate_middle(text, 20)

    assert cut.startswith("A" * 10)
    assert cut.endswith("B" * 10)


def test_truncate_middle_marks_the_seam_in_band():
    """Without an in-band marker the seam reads as continuous prose and a
    backtrace appears to step from one frame straight to an unrelated one."""
    cut = AssistantAgent._truncate_middle("A" * 100, 20)

    assert "80 characters dropped from the middle" in cut


def test_truncate_middle_keeps_exactly_the_budget_of_original_text():
    """`included_chars` in the tag is a promise about the original text, so
    the marker's own length must not be counted against it."""
    cut = AssistantAgent._truncate_middle("A" * 5000, 1000)
    marker_start = cut.index("[")
    marker_end = cut.index("]") + 1

    assert len(cut[:marker_start].rstrip("\n")) == 500
    assert len(cut[marker_end:].lstrip("\n")) == 500


def test_truncate_middle_counts_characters_not_bytes():
    """Byte-slicing UTF-8 splits codepoints; every other cap in the assistant
    counts characters, and this pipeline is explicitly multilingual."""
    text = "æ" * 100
    cut = AssistantAgent._truncate_middle(text, 20)

    assert cut.startswith("æ" * 10)
    assert "80 characters dropped" in cut


# --- what the prompts carry -----------------------------------------------


def test_request_inside_the_cap_travels_whole_and_unmarked():
    agent = _agent()
    prompt = agent._build_user_prompt(
        messages=[{"sender_type": "human", "text": "how tall is the tower?"}],
        scratchpad=[], step_index=0)

    assert "how tall is the tower?" in prompt
    assert "truncated=" not in prompt
    assert "current_user_request_summary_markdown" not in prompt


def test_long_request_is_cut_and_the_tag_says_so():
    agent = _agent()
    text = _paste(4000)
    prompt = agent._build_user_prompt(
        messages=[{"sender_type": "human", "text": text}],
        scratchpad=[], step_index=0)

    assert 'truncated="middle"' in prompt
    assert f'original_chars="{len(text)}"' in prompt
    assert f'included_chars="{agent.CURRENT_REQUEST_MAX_CHARS}"' in prompt
    assert "OPENING QUESTION" in prompt
    assert "CLOSING LINE" in prompt
    assert len(prompt) < len(text)


def test_summary_renders_beside_the_request_not_inside_its_attributes():
    """The shortening facts are code-owned and live in attributes; the
    description is model-written, so it gets its own section with its
    authority declared in the system prompt."""
    agent = _agent()
    agent._long_request_summary_markdown = agent._format_request_summary_markdown(
        _summary())
    prompt = agent._build_user_prompt(
        messages=[{"sender_type": "human", "text": _paste(4000)}],
        scratchpad=[], step_index=0)

    assert "<current_user_request_summary_markdown>" in prompt
    assert "a build log" in prompt
    assert "exit status 2" in prompt
    assert 'summary="' not in prompt
    assert prompt.index("<current_user_request") < prompt.index(
        "<current_user_request_summary_markdown>")


def test_every_prompt_that_carries_the_request_gets_the_same_treatment():
    """Five builders render current_user_request. A shortened request that
    reached only the decide loop would leave the reviewer and the auditor
    judging the reply against a request they think they read in full."""
    agent = _agent()
    agent._long_request_summary_markdown = agent._format_request_summary_markdown(
        _summary())
    messages = [{"sender_type": "human", "text": _paste(4000)}]

    prompts = [
        agent._build_user_prompt(
            messages=messages, scratchpad=[], step_index=0),
        agent._build_reply_audit_prompt(
            message="here you go", messages=messages, scratchpad=[]),
        agent._build_response_language_classifier_prompt(messages, None),
        agent._build_acceptance_criteria_prompt(messages),
    ]
    for prompt in prompts:
        assert 'truncated="middle"' in prompt
        assert "<current_user_request_summary_markdown>" in prompt


def test_history_messages_are_cut_too():
    """A paste one turn ago keeps arriving in every prompt of every later
    turn, where no summary call will ever describe it."""
    agent = _agent()
    old = _paste(4000)
    prompt = agent._build_user_prompt(
        messages=[
            {"sender_type": "human", "text": old},
            {"sender_type": "human", "text": "and the other one?"},
        ],
        scratchpad=[], step_index=0)

    history = prompt.split("<conversation_history_xml>")[1]
    assert 'truncated="middle"' in history
    assert f'included_chars="{agent.HISTORY_MESSAGE_MAX_CHARS}"' in history
    assert len(prompt) < len(old)


def test_the_summarizer_reads_far_more_than_the_prompts_do():
    """This call exists to see what the others cannot; a cap equal to theirs
    would make it describe only what they already have."""
    agent = _agent()
    text = _paste(3000)
    prompt = agent._build_request_summary_prompt(
        [{"sender_type": "human", "text": text}])

    assert text in prompt
    assert "truncated=" not in prompt


def test_the_summarizers_own_input_is_capped_and_marked():
    """The paste that triggers this call has no size limit. Its own middle
    goes the same way, and the tag says so rather than letting it describe a
    middle it never saw."""
    agent = _agent()
    text = _paste(20000)
    prompt = agent._build_request_summary_prompt(
        [{"sender_type": "human", "text": text}])

    assert 'truncated="middle"' in prompt
    assert f'included_chars="{agent.REQUEST_SUMMARY_INPUT_MAX_CHARS}"' in prompt


# --- the call ---------------------------------------------------------------


def test_the_summary_call_is_skipped_for_an_ordinary_request():
    """Latency spent on the rare turn, not on all of them."""
    agent = _agent()

    assert not agent._request_is_over_prompt_cap(
        [{"sender_type": "human", "text": "how tall is the tower?"}])
    assert agent._request_is_over_prompt_cap(
        [{"sender_type": "human", "text": _paste(4000)}])


def test_a_successful_call_injects_the_section_and_records_a_step():
    agent = _agent()
    agent._summarize_request = lambda **_: _summary()

    agent._run_request_summary_call(
        step_index=0, messages=[{"sender_type": "human",
                                 "text": _paste(4000)}])

    assert "a build log" in agent._long_request_summary_markdown
    assert agent._steps == [{
        "step_index": 0, "phase": "observed", "action": "request_summary",
        "reason": "long request summarized before step 0 (code-driven)",
        "error": None, "code_driven": True,
    }]


def test_a_failed_call_leaves_the_turn_running_on_the_shortened_request():
    """Fail-open: the request still travels, just without the description of
    what was dropped."""
    agent = _agent()

    def boom(**_):
        raise RuntimeError("model exploded")

    agent._summarize_request = boom
    agent._run_request_summary_call(
        step_index=0, messages=[{"sender_type": "human",
                                 "text": _paste(4000)}])

    assert agent._long_request_summary_markdown == ""
    assert agent._steps[0]["phase"] == "failed"
    assert "model exploded" in agent._steps[0]["error"]


def test_an_unbound_model_group_records_a_skip_not_a_failure():
    agent = _agent()
    agent._summarize_request = lambda **_: None

    agent._run_request_summary_call(
        step_index=0, messages=[{"sender_type": "human",
                                 "text": _paste(4000)}])

    assert agent._steps[0]["phase"] == "skipped"
    assert agent._steps[0]["error"] is None


def test_summary_markdown_collapses_free_text_to_one_line_each():
    """A model-written description must not forge headings or list items into
    the surrounding section."""
    agent = _agent()
    markdown = agent._format_request_summary_markdown(RequestSummary(
        content_type="a log",
        summary="First line.\n## Forged heading\n- forged item",
        key_details=["a\nb"],
    ))

    lines = markdown.splitlines()
    headings = [ln for ln in lines if ln.startswith("#")]
    assert headings == ["## Content type", "## Summary", "## Key details"]
    assert "First line. ## Forged heading - forged item" in lines
    assert "- a b" in lines


def test_summary_markdown_omits_the_details_heading_when_there_are_none():
    agent = _agent()
    markdown = agent._format_request_summary_markdown(RequestSummary(
        content_type="a log", summary="Nothing quotable in it.",
        key_details=[]))

    assert "Key details" not in markdown


# --- what the readers are told ----------------------------------------------


def test_every_prompt_that_can_receive_a_cut_request_explains_the_cut():
    """A reader that does not know the middle is missing reads the seam as
    continuous text and judges the request on material it never saw."""
    for prompt in (ASSISTANT_SYSTEM_PROMPT, SECOND_OPINION_SYSTEM_PROMPT,
                   REPLY_AUDIT_SYSTEM_PROMPT):
        assert TRUNCATED_REQUEST_SECTION in prompt


def test_the_reviewer_and_the_auditor_are_told_a_cut_is_not_a_defect():
    """Both judge the reply against the request. Neither can fault it for the
    part of the request neither of them was shown."""
    assert "never itself a ground to reject" in SECOND_OPINION_SYSTEM_PROMPT
    assert "only a defect when you can see" in REPLY_AUDIT_SYSTEM_PROMPT


def test_the_summarizer_is_told_never_to_describe_what_it_did_not_see():
    """It is the assistant's only account of the dropped middle, so an
    invention here is the one error nothing downstream can catch."""
    assert "Never describe a middle you" in REQUEST_SUMMARY_SYSTEM_PROMPT
    assert "only account of it" in REQUEST_SUMMARY_SYSTEM_PROMPT
