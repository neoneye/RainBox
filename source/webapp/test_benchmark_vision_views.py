"""The /benchmark_vision endpoints: the page renders, start validates its
input, results and reasoning read the stored trials."""

from unittest.mock import patch

from webapp.core import app

_TARGETS = [{"uuid": "7f38095f-3ca2-4852-8ef6-58746672e03f", "kind": "config", "label": "lfm3",
             "model_name": "lfm3-vl:2b", "provider": "ollama", "vision": True,
             "settings": "thinking: model default"}]


def test_page_lists_the_vision_targets_and_cases():
    with patch("webapp.benchmark_vision_views.vision_targets", return_value=_TARGETS):
        resp = app.test_client().get("/benchmark_vision")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "lfm3-vl:2b" in html
    assert "bottom_right" in html
    assert "/benchmark_vision/image.png" in html


def test_image_is_served():
    resp = app.test_client().get("/benchmark_vision/image.png")
    assert resp.status_code == 200 and resp.mimetype == "image/png"
    resp.close()


def test_start_without_targets_is_a_400():
    resp = app.test_client().post("/benchmark_vision/start", json={"targets": []})
    assert resp.status_code == 400


def test_start_passes_targets_and_settings_to_the_runner():
    with patch("webapp.benchmark_vision_views.vision_benchmark_runner.start",
               return_value=True) as start:
        resp = app.test_client().post("/benchmark_vision/start", json={
            "targets": [_TARGETS[0]["uuid"]], "trials": 2, "warmup": False})
    assert resp.get_json() == {"started": True}
    _app, uuids = start.call_args.args
    assert uuids == [_TARGETS[0]["uuid"]]
    assert start.call_args.kwargs == {"trials": 2, "warmup": False}


def test_state_and_results_are_json():
    client = app.test_client()
    assert "running" in client.get("/benchmark_vision/state").get_json()
    assert isinstance(client.get("/benchmark_vision/results").get_json(), dict)


def test_reasoning_needs_a_uuid():
    client = app.test_client()
    assert client.get("/benchmark_vision/reasoning?uuid=nope").status_code == 400
    missing = "00000000-0000-0000-0000-000000000000"
    assert client.get(f"/benchmark_vision/reasoning?uuid={missing}").status_code == 404
