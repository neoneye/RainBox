"""Marker tests for webapp/model_group_views.py (the /modelgroup page shell;
its JS is inline in the rendered template, so a GET carries both markup and
behavior — same idea as test_chat_views)."""

from webapp.core import app  # noqa: F401  ensure routes register
import webapp  # noqa: F401  registers model_group_views on the shared app


def _body() -> str:
    return app.test_client().get("/modelgroup").get_data(as_text=True)


def test_legacy_plural_path_redirects_to_singular():
    # The page moved from /modelgroups to /modelgroup (singular, like /cron
    # and /kanban); old links redirect and keep their ?id= selection.
    resp = app.test_client().get("/modelgroups?id=abc")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/modelgroup?id=abc")


def test_group_rows_are_real_links():
    # Group rows in the left list are anchors with a real href so CMD/Ctrl
    # click (and middle click) opens the group in a new tab via its ?id= deep
    # link. Plain clicks are still intercepted for in-page selection; modified
    # clicks pass through to the browser.
    body = _body()
    assert 'href="?id=${encodeURIComponent(g.uuid)}"' in body
    assert "if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;" in body


def test_the_new_group_form_puts_systemone_first_and_locks_structured_output():
    # SystemOne decides what the choices under it can be, so it is the first
    # capability. A SystemOne model's output is always structured: choosing
    # "Must have" sets structured output to "Must have" and disables it.
    body = _body()
    assert body.index("capabilityField('systemone', 'SystemOne'") < body.index(
        "capabilityField('fc', 'Function calling'")
    assert "if (locked) r.checked = r.value === 'must_have';" in body
    assert "r.disabled = locked;" in body
    assert "if (e.target && e.target.name === 'new-systemone') syncSystemOne();" in body
    assert "systemone_constraint: systemone" in body


def test_only_a_decision_slot_is_offered_a_systemone_group():
    from webapp.agent_views import _agent_group_options

    groups = [
        {"uuid": "a", "requires_function_calling": False, "requires_structured_output": True,
         "structured_output_constraint": "must_have", "requires_systemone": True},
        {"uuid": "b", "requires_function_calling": False, "requires_structured_output": True,
         "structured_output_constraint": "must_have", "requires_systemone": False},
    ]
    decision = {"requires_systemone": True}
    generative = {"requires_structured_output": True}
    assert [o["uuid"] for o in _agent_group_options(decision, groups)] == ["a"]
    assert [o["uuid"] for o in _agent_group_options(generative, groups)] == ["b"]
