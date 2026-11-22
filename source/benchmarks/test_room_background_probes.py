"""The probe room builds, and its prompts differ by the background alone.
No model is called."""
from __future__ import annotations

from benchmarks import room_background_probes as probes


def test_the_room_has_a_background_that_holds_what_the_probes_ask_for():
    messages, background = probes.build_room()
    assert len(messages) == 120
    assert "the rowing club newsletter" in background.topics
    for said in ("the shed has no power", "odd plots on Mondays", "the early shift"):
        assert said in background.earlier
    assert "Skerry Point lighthouse was built. the answer was rejected." in background.earlier
    # None of it is in the window, which is what a turn without it reads.
    window = " ".join(m["text"] for m in messages[-30:])
    assert "seed list" not in window and "newsletter" not in window


def test_a_probe_is_the_same_decide_call_with_and_without_the_blocks():
    messages, background = probes.build_room()
    request = probes.PROBES[0][1]
    off = probes.decide_prompt(messages, request, None)
    on = probes.decide_prompt(messages, request, background)
    assert off[-1] == on[-1] == {"role": "user", "content": request}
    assert "<room_earlier>" in on[1]["content"] and "room_" not in off[1]["content"]
    assert on[1]["content"].endswith(off[1]["content"][off[1]["content"].index(
        "<conversation_history_xml>"):])
    # A read the turn made is in the next call.
    step = {"action": "memory_query", "args": {}, "reason": "look",
            "observation": probes.OBSERVATIONS[""]}
    assert "Nothing was found." in probes.decide_prompt(
        messages, request, background, [step])[1]["content"]
