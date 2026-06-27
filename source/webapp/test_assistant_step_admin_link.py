"""The flask-admin AssistantStep list links each step's uuid to its /assistant
trace location (?id=<run>#step-<uuid>)."""
from uuid import uuid4

from webapp.core import _format_step_trace_link


class _FakeStep:
    def __init__(self, run_uuid, uuid):
        self.run_uuid = run_uuid
        self.uuid = uuid


def test_trace_link_points_at_run_and_step():
    run_uuid, step_uuid = uuid4(), uuid4()
    html = str(_format_step_trace_link(None, None,
                                       _FakeStep(run_uuid, step_uuid), "uuid"))
    assert f"/assistant?id={run_uuid}#step-{step_uuid}" in html
    assert str(step_uuid) in html      # uuid still shown as the link text
    assert html.startswith("<a ")
