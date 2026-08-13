"""Prompt tier order: static head, turn_instructions, dynamic tail.

The tiers exist so the local backends have a stable prompt prefix to reuse
across steps and across the six assistant calls. Order is the whole property,
so it gets asserted directly rather than implied by content tests.
"""
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
    return agent


ALL_TURN_INSTRUCTIONS = [
    DECIDE_TURN_INSTRUCTIONS,
    ACCEPTANCE_CRITERIA_TURN_INSTRUCTIONS,
    SECOND_OPINION_TURN_INSTRUCTIONS,
    REPLY_AUDIT_TURN_INSTRUCTIONS,
    RESPONSE_LANGUAGE_TURN_INSTRUCTIONS,
    REQUEST_SUMMARY_TURN_INSTRUCTIONS,
]


def test_shared_system_prompt_names_turn_instructions_as_sole_authority():
    assert "turn_instructions" in ASSISTANT_SHARED_SYSTEM_PROMPT
    # The truncated-request rule was duplicated into four per-call prompts.
    assert 'truncated="middle"' in ASSISTANT_SHARED_SYSTEM_PROMPT


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
    # tier 1
    "user_settings_json", "assistant_persona", "formatting_guide",
    "knowledge_calibration", "user_profile",
    # tier 2
    "turn_instructions",
    # tier 3
    "active_skills", "conversation_history_xml", "reply_language_markdown",
    "acceptance_criteria_markdown", "current_turn_steps",
    "current_user_request", "decision_request", "current_local_time",
]


def test_decide_prompt_follows_tier_order(fully_populated_agent):
    prompt = fully_populated_agent._build_user_prompt(
        messages=[{"sender_type": "human", "text": "what is my mother called"}],
        scratchpad=[], step_index=0)

    order = section_order(prompt)
    assert order == [s for s in DECIDE_EXPECTED if s in order]
    assert order.count("turn_instructions") == 1


def test_decide_prompt_ends_with_request_then_clock(fully_populated_agent):
    prompt = fully_populated_agent._build_user_prompt(
        messages=[{"sender_type": "human", "text": "hello"}],
        scratchpad=[], step_index=0)

    order = section_order(prompt)
    assert order[-1] == "current_local_time"
    assert "current_user_request" in order
    assert order.index("current_user_request") > order.index("turn_instructions")
