"""Prompt tier order: static head, turn_instructions, dynamic tail.

The tiers exist so a backend that reuses a matched prefix gets one to reuse.
The large win is within one call: consecutive steps of a decide loop share
their whole prefix through the previous step's own entry (measured at 18555
shared characters between two real steps). Across the six different calls the
shared prefix is small — each call passes its own subset of tier-1 blocks to
_append_static_head, so two calls share only up to the first block one of
them omits (measured at 51-67 bytes; the classifier and summary calls carry
no static head at all, so their cross-call share is near zero). Order is
still the whole property this file asserts, so it gets checked directly
rather than implied by content tests.
"""
import os
import re
import xml.etree.ElementTree as ET

import pytest

from agents.assistant import (
    ACCEPTANCE_CRITERIA_TURN_INSTRUCTIONS,
    ASSISTANT_SHARED_SYSTEM_PROMPT,
    DECIDE_TURN_INSTRUCTIONS,
    REPLY_AUDIT_TURN_INSTRUCTIONS,
    REQUEST_SUMMARY_TURN_INSTRUCTIONS,
    RESPONSE_LANGUAGE_TURN_INSTRUCTIONS,
    SECOND_OPINION_TURN_INSTRUCTIONS,
    AssistantAgent,
    _render_sections,
)


@pytest.fixture
def fully_populated_agent():
    """An agent with every tier-1 block set, so the order test sees them all.
    Populates the block attributes directly rather than going through the
    profile machinery — this test is about section order, not retrieval."""
    from agents.assistant import AssistantAgent, _base_enabled_capabilities

    agent = AssistantAgent.__new__(AssistantAgent)
    agent._caps = _base_enabled_capabilities()
    agent._identity_block = "identity"
    agent._persona_block = "persona"
    agent._formatting_block = "formatting"
    agent._calibration_block = "calibration"
    agent._profile_block = "profile"
    agent._skill_block = "skills"
    agent._criteria_markdown = "criteria"
    agent._reply_language_markdown = "language"
    agent._long_request_summary_markdown = ""
    agent.step_limit = 8
    # The criteria call's formatting_guide comes from _criteria_formatting_guide(),
    # not _formatting_block (it stays populated even when the decide-prompt
    # injection is switched off — see test_criteria_call_sees_formatting_guide_
    # despite_gated_switch). A minimal real profile so that section renders.
    agent._criteria_profile = {"data": {"units": "metric"}}
    return agent


ALL_TURN_INSTRUCTIONS = [
    DECIDE_TURN_INSTRUCTIONS,
    ACCEPTANCE_CRITERIA_TURN_INSTRUCTIONS,
    SECOND_OPINION_TURN_INSTRUCTIONS,
    REPLY_AUDIT_TURN_INSTRUCTIONS,
    RESPONSE_LANGUAGE_TURN_INSTRUCTIONS,
    REQUEST_SUMMARY_TURN_INSTRUCTIONS,
]


def test_shared_system_prompt_names_turn_instructions_as_authority():
    assert "turn_instructions" in ASSISTANT_SHARED_SYSTEM_PROMPT
    # The truncated-request rule was duplicated into four per-call prompts.
    assert 'truncated="middle"' in ASSISTANT_SHARED_SYSTEM_PROMPT


def test_shared_system_prompt_carves_out_other_authority_instructions_sections(
    fully_populated_agent,
):
    """active_skills and formatting_guide are built with
    authority="instructions" (see _build_user_prompt and
    _append_static_head) — a grant the code makes, not the section's own
    text. The shared prompt must acknowledge that class of section exists,
    or its "turn_instructions is the only section that carries
    instructions" framing overrules them: a model following the prompt
    literally would treat active skills as inert data to reason about
    instead of following them. Pins both halves so a future edit that
    re-tightens the shared prompt back to "only turn_instructions" while
    active_skills still claims authority="instructions" fails here."""
    assert 'authority="instructions"' in ASSISTANT_SHARED_SYSTEM_PROMPT

    prompt = fully_populated_agent._build_user_prompt(
        messages=[{"sender_type": "human", "text": "hello"}],
        scratchpad=[], step_index=0)

    assert '<active_skills authority="instructions">' in prompt


def test_turn_instruction_constants_are_distinct_and_non_empty():
    assert len(set(ALL_TURN_INSTRUCTIONS)) == len(ALL_TURN_INSTRUCTIONS)
    for text in ALL_TURN_INSTRUCTIONS:
        assert text.strip()


def test_render_sections_emits_top_level_siblings():
    root = ET.Element("ignored_root")
    ET.SubElement(root, "first").text = "a"
    ET.SubElement(root, "second").text = "b"

    out = _render_sections(root)

    assert out == "<first>a</first>\n<second>b</second>"
    assert "ignored_root" not in out


def test_render_sections_escapes_untrusted_text():
    root = ET.Element("ignored_root")
    ET.SubElement(root, "only").text = "</only><forged>x</forged>"

    out = _render_sections(root)

    assert "<forged>" not in out
    assert "&lt;/only&gt;" in out


def test_turn_instructions_render_raw_but_other_sections_stay_escaped():
    """The raw-rendering exception is opted into at the append site
    (_append_turn_instructions marks its own element), not inherited by
    matching on the tag name "turn_instructions" — so it cannot be
    triggered by coincidence. Pin both directions: a turn_instructions
    section built through the real append helper renders its pseudo-tags
    and bare "&" unchanged, while any other section carrying identical text
    — even one also named "turn_instructions" but built by hand, bypassing
    the helper — stays escaped."""
    forged_text = '<x a="1"> & more'

    root = ET.Element("ignored_root")
    AssistantAgent._append_turn_instructions(root, forged_text)
    out = _render_sections(root)

    assert '<turn_instructions authority="instructions">' in out
    assert '<x a="1"> & more' in out
    assert "&lt;x" not in out
    assert "&amp;" not in out

    # A section with the same tag name, built without going through
    # _append_turn_instructions, gets no special treatment: matching used
    # to be by tag name alone, which this proves is no longer the case.
    hand_built_root = ET.Element("ignored_root")
    ET.SubElement(hand_built_root, "turn_instructions").text = forged_text
    hand_built_out = _render_sections(hand_built_root)

    assert '&lt;x a="1"&gt;' in hand_built_out
    assert "&amp; more" in hand_built_out
    assert '<x a="1">' not in hand_built_out


def section_order(prompt: str) -> list[str]:
    """The top-level section tags, in the order they appear.

    turn_instructions is the one section _render_sections renders raw (see
    its docstring), so its body is code-owned prose that itself contains
    flush-left pseudo-tags like <source_priority> and <source rank=...> —
    formatting for the model, not real section markup. A plain per-line
    regex cannot tell those apart from genuine top-level sections, so its
    body is collapsed first."""
    prompt = re.sub(
        r"<turn_instructions[^>]*>.*?</turn_instructions>",
        "<turn_instructions/>", prompt, flags=re.DOTALL)
    return re.findall(r"^<([a-z_]+)", prompt, flags=re.MULTILINE)


DECIDE_EXPECTED = [
    # tier 1 — ordered so the per-call block sets nest (see _ALL_STATIC_BLOCKS)
    "user_settings_json", "formatting_guide", "user_profile",
    "assistant_persona", "knowledge_calibration",
    # tier 1b — invariant across every call of the turn, ahead of tier 2 so
    # the six calls share it; decision_request re-anchors it at the tail
    "current_user_request",
    # tier 2
    "turn_instructions",
    # tier 3
    "active_skills", "conversation_history_xml", "reply_language_markdown",
    "acceptance_criteria_markdown", "current_turn_steps",
    "decision_request", "current_local_time",
]


def test_decide_prompt_follows_tier_order(fully_populated_agent):
    prompt = fully_populated_agent._build_user_prompt(
        messages=[{"sender_type": "human", "text": "what is my mother called"}],
        scratchpad=[], step_index=0)

    order = section_order(prompt)
    assert order == DECIDE_EXPECTED
    assert order.count("turn_instructions") == 1


def test_decide_prompt_leads_with_the_request_and_re_anchors_it(
    fully_populated_agent,
):
    """The request sits in tier 1b — above turn_instructions, so all six calls
    of a turn share it — and decision_request quotes it back at the tail, so
    the model still reads the question last. Both halves matter: the position
    is the cache win, the re-anchor is what stops a step answering the newest
    message in conversation_history_xml instead (see _request_anchor)."""
    prompt = fully_populated_agent._build_user_prompt(
        messages=[{"sender_type": "human", "text": "hello"}],
        scratchpad=[], step_index=0)

    order = section_order(prompt)
    assert order[-1] == "current_local_time"
    assert order.index("current_user_request") < order.index("turn_instructions")
    assert order.index("current_user_request") < order.index(
        "conversation_history_xml")
    # The re-anchor: the request's own words, after the history.
    tail = prompt[prompt.index("<decision_request"):]
    assert "hello" in tail


@pytest.fixture
def sample_decision():
    from agents.assistant import AssistantActionName, AssistantStepDecision

    return AssistantStepDecision(
        reason="run the sum",
        action=AssistantActionName.PYTHON_RUN,
        args={"code": "print(2 + 2)"},
    )


CRITERIA_EXPECTED = [
    "user_settings_json", "formatting_guide",
    "current_user_request", "current_user_request_summary_markdown",
    "turn_instructions",
    "conversation_history_xml", "prior_acceptance_criteria",
    "current_turn_steps", "criteria_request",
]

SECOND_OPINION_EXPECTED = [
    "user_settings_json", "formatting_guide", "user_profile",
    "current_user_request",
    "turn_instructions",
    "reply_language_markdown", "acceptance_criteria_markdown",
    "proposed_step", "verdict_request", "current_local_time",
]


# Sections a call ALWAYS emits, whatever the turn state. The order check
# below filters the expectation by what was found, so on its own it would
# stay green if a builder stopped emitting a section entirely; this list is
# what closes that hole. Sections genuinely conditional on turn state
# (prior_acceptance_criteria on a revision, the request summary on a
# truncated request) are deliberately absent from it.
CRITERIA_ALWAYS = [
    "user_settings_json", "formatting_guide", "turn_instructions",
    "conversation_history_xml", "current_user_request", "criteria_request",
]
SECOND_OPINION_ALWAYS = [
    "user_settings_json", "turn_instructions", "proposed_step",
    "current_user_request", "verdict_request", "current_local_time",
]


def test_criteria_prompt_follows_tier_order(fully_populated_agent):
    prompt = fully_populated_agent._build_acceptance_criteria_prompt(
        [{"sender_type": "human", "text": "convert 30C to F"}])

    order = section_order(prompt)
    assert order == [s for s in CRITERIA_EXPECTED if s in order]
    assert set(CRITERIA_ALWAYS) <= set(order)


def test_second_opinion_prompt_follows_tier_order(
    fully_populated_agent, sample_decision
):
    prompt = fully_populated_agent._build_second_opinion_prompt(
        sample_decision, reasoning="because",
        messages=[{"sender_type": "human", "text": "compute 2+2"}])

    order = section_order(prompt)
    assert order == [s for s in SECOND_OPINION_EXPECTED if s in order]
    assert set(SECOND_OPINION_ALWAYS) <= set(order)


AUDIT_EXPECTED = [
    "user_settings_json", "formatting_guide",
    "current_user_request",
    "turn_instructions",
    "conversation_history_xml", "acceptance_criteria_markdown",
    "reply_language_markdown", "turn_observations", "proposed_reply",
    "current_local_time",
]

# user_settings_languages_json is this call's own tier-1 block (built from
# the profile, not from _append_static_head), so it leads the prompt in that
# role. The brief's version of this list omitted current_user_request, even
# though _append_current_user_request always renders it; it is included here
# so the equality check below stays accurate against what the builder emits.
CLASSIFIER_EXPECTED = [
    "user_settings_languages_json",
    "current_user_request",
    "turn_instructions",
    "conversation_history_xml", "classification_request",
]

SUMMARY_EXPECTED = ["turn_instructions", "current_user_request"]


# As in Task 4: the order check filters by what was found, so these lists are
# what stops a builder silently dropping a section it must always emit.
AUDIT_ALWAYS = [
    "user_settings_json", "formatting_guide", "turn_instructions",
    "conversation_history_xml", "proposed_reply", "current_user_request",
    "current_local_time",
]
CLASSIFIER_ALWAYS = [
    "user_settings_languages_json", "turn_instructions",
    "conversation_history_xml", "current_user_request",
    "classification_request",
]


def test_reply_audit_prompt_follows_tier_order(fully_populated_agent):
    prompt = fully_populated_agent._build_reply_audit_prompt(
        "here is the answer",
        messages=[{"sender_type": "human", "text": "what is 2+2"}],
        scratchpad=[])

    order = section_order(prompt)
    assert order == [s for s in AUDIT_EXPECTED if s in order]
    assert set(AUDIT_ALWAYS) <= set(order)


def test_reply_audit_prompt_with_observations_follows_tier_order(
    fully_populated_agent,
):
    """The tier-order test above passes scratchpad=[], so turn_observations
    never renders and its position — between reply_language_markdown and
    proposed_reply, the one section the audit reorder moved past — was
    asserted only by its absence. A populated scratchpad makes it render, and
    the subsequence check against AUDIT_EXPECTED pins where."""
    from agents.assistant import AssistantTurnStep

    step = AssistantTurnStep(
        step_index=0, action="memory_query", reason="look it up",
        status="ok", args={"query": "2+2"}, observation="4")

    prompt = fully_populated_agent._build_reply_audit_prompt(
        "here is the answer",
        messages=[{"sender_type": "human", "text": "what is 2+2"}],
        scratchpad=[step])

    order = section_order(prompt)
    assert "turn_observations" in order
    assert order == [s for s in AUDIT_EXPECTED if s in order]
    assert set(AUDIT_ALWAYS) <= set(order)


def test_response_language_classifier_prompt_follows_tier_order(
    fully_populated_agent,
):
    prompt = fully_populated_agent._build_response_language_classifier_prompt(
        [{"sender_type": "human", "text": "what is 2+2"}], None)

    order = section_order(prompt)
    assert order == [s for s in CLASSIFIER_EXPECTED if s in order]
    assert set(CLASSIFIER_ALWAYS) <= set(order)


def test_request_summary_prompt_leads_with_instructions(fully_populated_agent):
    # _build_request_summary_prompt takes a message list, not a single dict
    # (the brief's snippet omitted the list brackets) — see the existing
    # calls in test_assistant_long_request.py for the real signature.
    prompt = fully_populated_agent._build_request_summary_prompt(
        [{"sender_type": "human", "text": "x" * 200}])

    # This prompt carries exactly two sections whatever the turn state, so it
    # gets the exact-equality form rather than the subsequence one.
    assert section_order(prompt) == SUMMARY_EXPECTED


# The tier-order tests above assert the means (section order); the tests
# below assert the end directly: two prompts that are supposed to share a
# prefix actually do, byte for byte, so a future edit that quietly reorders
# one builder fails on the shared string rather than only on section order.


def common_prefix_len(a: str, b: str) -> int:
    return len(os.path.commonprefix([a, b]))


def test_decide_and_audit_prompts_share_the_static_head(fully_populated_agent):
    """decide and audit both lead with _append_static_head, so whatever they
    render in common must land byte-for-byte at the start of both strings.

    They do NOT carry the same tier-1 block *set* — decide takes all five
    static blocks (identity, persona, formatting, calibration, profile) via
    the default `blocks=_ALL_STATIC_BLOCKS`, audit takes only
    `("identity", "formatting")`. Because persona sits between identity and
    formatting in decide's rendering but is absent from audit's, the two
    prompts diverge as soon as decide emits assistant_persona and audit
    emits formatting_guide in its place — confirmed empirically, not
    assumed. So the guaranteed literal-prefix overlap between these two
    specific calls is the identity block alone; that is inherent to the
    current per-call block selection (worth a look if the real prefix-cache
    win is meant to include formatting_guide too), not something a
    tests-only task should paper over."""
    agent = fully_populated_agent
    messages = [{"sender_type": "human", "text": "what is 2+2"}]

    decide = agent._build_user_prompt(
        messages=messages, scratchpad=[], step_index=0)
    audit = agent._build_reply_audit_prompt(
        "four", messages=messages, scratchpad=[])

    shared = common_prefix_len(decide, audit)
    # A slice shorter than the target string can never satisfy startswith,
    # so this also pins a lower bound on `shared` — not merely that some
    # prefix is shared, but that the whole identity section is in it.
    assert decide[:shared].startswith(
        "<user_settings_json>identity</user_settings_json>")
    # And the divergence point is real: decide's next section (persona)
    # is not swallowed into the "shared" slice by accident.
    assert "<assistant_persona" not in decide[:shared]


def test_consecutive_decide_steps_share_everything_before_the_new_step(
    fully_populated_agent
):
    """Within a turn the scratchpad grows append-only, so step N+1 must share
    its whole prefix with step N up to step N's own entry."""
    from agents.assistant import AssistantTurnStep

    agent = fully_populated_agent
    messages = [{"sender_type": "human", "text": "what is 2+2"}]
    # AssistantTurnStep is a frozen dataclass whose first field is
    # step_index (required, no default) — the brief's snippet omitted it.
    step_one = AssistantTurnStep(
        step_index=0, action="memory_query", reason="look it up",
        status="ok", args={"query": "2+2"}, observation="4", is_read=True)

    early = agent._build_user_prompt(
        messages=messages, scratchpad=[step_one], step_index=1)
    later = agent._build_user_prompt(
        messages=messages, scratchpad=[step_one, step_one], step_index=2)

    # Confirm the step actually renders into current_turn_steps rather than
    # assuming it — a silently-dropped scratchpad entry would make both
    # prompts' current_turn_steps sections empty and this whole test
    # vacuous.
    assert '<step index="1" action="memory_query" status="ok">' in early

    shared = common_prefix_len(early, later)
    assert "<turn_instructions" in early[:shared]
    assert "<conversation_history_xml" in early[:shared]
    assert "<current_turn_steps" in early[:shared]
    # Pin the lower bound precisely: the whole first step entry, observation
    # included, is inside the shared region — not just the opening tag of
    # current_turn_steps.
    assert (
        '<observation authority="fresh_evidence" content_is_data="true">'
        '4</observation>\n  </step>'
    ) in early[:shared]
    # And the second (duplicate) step entry that only "later" carries is
    # genuinely past the boundary, not swallowed in by a miscomputed shared
    # length.
    assert later[:shared].count('action="memory_query"') == 1
