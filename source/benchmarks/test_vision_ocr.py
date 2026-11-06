import base64

from benchmarks.vision_ocr import (
    CASES,
    IMAGE_PATH,
    IMAGE_WORDS,
    also_named,
    completion_body,
    contains_word,
    missing_words,
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


def test_completion_body_carries_prompts_and_png():
    case = CASES[1]
    png = IMAGE_PATH.read_bytes()
    body = completion_body("m", case, png)
    system, user = body["messages"]
    assert system == {"role": "system", "content": case.system}
    text, image = user["content"]
    assert text == {"type": "text", "text": case.user}
    url = image["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == png


def test_request_base_url_prefers_api_base():
    assert request_base_url("jan", {"api_base": "http://h:1337/v1/"}) == "http://h:1337/v1"


def test_request_base_url_appends_v1_to_the_provider_base():
    base = request_base_url("ollama", {"base_url": "http://127.0.0.1:11434"})
    assert base is not None and base.endswith("/v1")


def test_request_base_url_unknown_provider():
    assert request_base_url("nope", {}) is None
