import base64
import json

from benchmarks.vision_ocr import (
    CASES,
    IMAGE_PATH,
    IMAGE_WORDS,
    Target,
    _Clock,
    also_named,
    contains_word,
    estimate_reasoning_tokens,
    missing_words,
    ollama_body,
    ollama_options,
    openai_body,
    openai_options,
    parse_ollama_stream,
    parse_openai_stream,
)
from providers import request_base_url


def test_contains_word_ignores_case_and_formatting():
    assert contains_word("The center says **TESTING**.", "Testing")
    assert contains_word("- `cat`\n- bug", "cat")


def test_contains_word_rejects_a_longer_word():
    assert not contains_word("a category", "cat")
    assert not contains_word("dogs", "dog")


def test_all_texts_passes_with_any_order_and_extra_words():
    case = next(c for c in CASES if c.name == "all_texts")
    answer = "I can see: dog, ant, Testing (center), bug and cat."
    assert missing_words(answer, case.expected) == []


def test_all_texts_names_the_missing_word():
    case = next(c for c in CASES if c.name == "all_texts")
    assert missing_words("cat bug Testing dog", case.expected) == ["ant"]


def test_location_case_reports_other_words_without_failing():
    case = next(c for c in CASES if c.name == "top_left")
    answer = "Top left: cat. Other text: bug, Testing."
    assert missing_words(answer, case.expected) == []
    assert also_named(answer, case.expected) == ["bug", "Testing"]


def test_every_case_expects_words_from_the_image():
    for case in CASES:
        assert set(case.expected) <= set(IMAGE_WORDS)


def _target(transport, options=None):
    return Target("u", "label", "ollama", "m", transport, "http://x", {}, options or {})


def test_openai_body_carries_prompts_png_and_options():
    case = CASES[1]
    png = IMAGE_PATH.read_bytes()
    body = openai_body(_target("openai", {"temperature": 0.2}), case, png)
    system, user = body["messages"]
    assert system == {"role": "system", "content": case.system}
    text, image = user["content"]
    assert text == {"type": "text", "text": case.user}
    url = image["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == png
    assert body["temperature"] == 0.2
    assert body["stream_options"] == {"include_usage": True}


def test_ollama_body_sends_the_image_beside_the_prompt():
    case = CASES[2]
    png = IMAGE_PATH.read_bytes()
    body = ollama_body(_target("ollama", {"think": False}), case, png)
    user = body["messages"][1]
    assert user["content"] == case.user
    assert base64.b64decode(user["images"][0]) == png
    assert body["think"] is False


def test_ollama_options_map_the_config_settings():
    assert ollama_options({"thinking": True, "temperature": 0.2, "context_window": 8192,
                           "max_tokens": 512, "base_url": "http://h"}) == {
        "think": True,
        "options": {"temperature": 0.2, "num_ctx": 8192, "num_predict": 512},
    }


def test_ollama_options_leave_thinking_to_the_model_when_unset():
    assert "think" not in ollama_options({"temperature": 0.2})


def test_openai_options_merge_extra_body():
    args = {"temperature": 0.15,
            "additional_kwargs": {"extra_body": {"reasoning": {"effort": "high"}}}}
    assert openai_options(args) == {"temperature": 0.15, "reasoning": {"effort": "high"}}


def test_parse_ollama_stream_reads_text_counts_and_durations():
    lines = [json.dumps(x).encode() for x in (
        {"message": {"role": "assistant", "content": "", "thinking": "look"}, "done": False},
        {"message": {"role": "assistant", "content": "cat"}, "done": False},
        {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop",
         "prompt_eval_count": 150, "prompt_eval_cached_count": 4, "eval_count": 20,
         "load_duration": 2_000_000, "prompt_eval_duration": 250_000_000,
         "eval_duration": 500_000_000},
    )]
    reply = parse_ollama_stream(iter(lines), _Clock())
    assert (reply.content, reply.reasoning) == ("cat", "look")
    assert (reply.prompt_tokens, reply.cached_tokens, reply.output_tokens) == (150, 4, 20)
    assert reply.prefill_s == 0.25 and reply.decode_s == 0.5 and reply.load_s == 0.002
    assert reply.decode_tps == 40
    assert reply.done_reason == "stop"
    assert reply.first_token is not None and reply.thinking is not None


def test_parse_openai_stream_reads_usage_and_timings():
    chunks = [
        {"choices": [{"delta": {"content": "", "reasoning": "hmm"}}]},
        {"choices": [{"delta": {"content": "dog"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 290, "completion_tokens": 10,
                                  "prompt_tokens_details": {"cached_tokens": 289}},
         "timings": {"prompt_ms": 0, "predicted_ms": 200}},
    ]
    lines = [b"data: " + json.dumps(c).encode() for c in chunks] + [b"data: [DONE]"]
    reply = parse_openai_stream(iter(lines), _Clock())
    assert (reply.content, reply.reasoning) == ("dog", "hmm")
    assert (reply.prompt_tokens, reply.cached_tokens, reply.output_tokens) == (290, 289, 10)
    # A zero prompt_ms is "not measured" (a full cache hit), not a figure.
    assert reply.prefill_s is None and reply.decode_s == 0.2
    assert reply.done_reason == "stop"


def test_a_reply_without_reasoning_spent_no_time_thinking():
    lines = [json.dumps({"message": {"content": "ant"}, "done": True}).encode()]
    reply = parse_ollama_stream(iter(lines), _Clock())
    assert reply.thinking == 0.0


def test_estimate_reasoning_tokens_splits_by_characters():
    assert estimate_reasoning_tokens(100, "x" * 300, "y" * 100) == 75
    assert estimate_reasoning_tokens(100, "", "answer") == 0
    assert estimate_reasoning_tokens(None, "x", "y") is None


def test_request_base_url_prefers_api_base():
    assert request_base_url("jan", {"api_base": "http://h:1337/v1/"}) == "http://h:1337/v1"


def test_request_base_url_appends_v1_to_the_provider_base():
    base = request_base_url("ollama", {"base_url": "http://127.0.0.1:11434"})
    assert base is not None and base.endswith("/v1")


def test_request_base_url_unknown_provider():
    assert request_base_url("nope", {}) is None
