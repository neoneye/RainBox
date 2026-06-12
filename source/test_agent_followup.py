"""Integration tests for FollowUpClassifierAgent (agent_followup.py).

These make a real structured-output call to LM Studio, so they need a model
group assigned to the "followup" agent first:

    Open http://127.0.0.1:5000/agent_models and assign a model group to the
    "followup" agent.

If no group is assigned (or LM Studio is unreachable) the tests skip rather
than fail, so they're safe to run unattended.

Run with the venv's interpreter so the right pytest/psycopg are used:

    python -m pytest test_agent_followup.py -v

(A bare `pytest` may resolve to a different Python on your PATH that lacks
psycopg — `python -m pytest` avoids that.)
"""

import pytest

import db
import providers
from agent_config import FOLLOWUP_UUID
from agent_followup import FollowUpClassifierAgent


@pytest.fixture(scope="module")
def classifier():
    """A set-up FollowUpClassifierAgent for the 'followup' agent, or a skip if
    its preconditions (assigned model group, reachable LM Studio) aren't met."""
    app = db.make_app()
    db.init_db(app)  # ensures schema + the 'followup' binding row exist
    ctx = app.app_context()
    ctx.push()
    try:
        binding = db.get_agent_model_binding(FOLLOWUP_UUID)
        group_uuid = binding.model_group_uuid if binding is not None else None
        if group_uuid is None:
            pytest.skip(
                "no model group assigned to the 'followup' agent — assign one at "
                "http://127.0.0.1:5000/agent_models before running these tests"
            )
            return
        if not db.get_model_group_member_uuids(group_uuid):
            pytest.skip("the 'followup' agent's model group has no models")
        lm = providers.get("lm_studio")
        try:
            lm.list_models()
        except Exception as e:
            pytest.skip(f"LM Studio unreachable at {lm.base_url()}: {e}")

        agent = FollowUpClassifierAgent(
            agent_uuid=FOLLOWUP_UUID, name="followup", send=lambda m: None
        )
        agent.setup()
        yield agent
    finally:
        ctx.pop()


def _classify(agent: FollowUpClassifierAgent, message: str) -> str:
    result = agent.handle(0, {"message": message})
    return result["response"]["needs_response"]


@pytest.mark.parametrize(
    "message, expected",
    [
        ("Thanks!", "no"),
        ("Channel created.", "no"),
        ("nice", "no"),
        ("Hoping you are enjoying the summer", "no"),
        ("Can someone check the benchmark logs?", "yes"),
        ("What size shoes are you using Alice?", "yes"),
        ("Cool! Alice?", "yes"),
        ("When should we meet?", "yes"),
    ],
)
def test_clear_cases(classifier: FollowUpClassifierAgent, message: str, expected: str):
    assert _classify(classifier, message) == expected


def test_bare_yes_is_ambiguous(classifier: FollowUpClassifierAgent):
    # "yes" on its own is ambiguous; the prompt steers toward "maybe", but a
    # model could also reasonably answer "yes". Either is acceptable; "no" isn't.
    assert _classify(classifier, "yes") in ("maybe", "yes")


def test_always_valid_literal(classifier: FollowUpClassifierAgent):
    verdict = _classify(classifier, "Could you review my PR when you get a chance?")
    assert verdict in ("yes", "maybe", "no")
