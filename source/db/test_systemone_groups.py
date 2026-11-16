"""SystemOne (decision) models and the model groups that hold them: what
the provider says a model is, what a group admits, and which agents may
bind it."""
import pytest

from db import (
    ModelConfig, create_model_group, db, delete_model_group, init_db, make_app,
    member_is_systemone, set_model_group_members, sync_model_configs,
)

PROVIDER = "systemone-test"     # no real provider: the sync touches these rows only


@pytest.fixture
def models():
    app = make_app()
    init_db(app)
    with app.app_context():
        sync_model_configs(
            provider=PROVIDER, available_model_names=["decider", "writer"],
            default_arguments={"should_use_structured_outputs": True},
            systemone_by_name={"decider": True, "writer": False})
        rows = {r.model_name: r for r in db.session.query(ModelConfig).filter_by(provider=PROVIDER)}
        made = []
        try:
            yield rows, made
        finally:
            for g in made:
                delete_model_group(g.uuid)
            db.session.query(ModelConfig).filter_by(provider=PROVIDER).delete()
            db.session.commit()


def test_the_sync_records_what_the_provider_says_a_model_is(models):
    rows, _ = models
    assert (rows["decider"].systemone, rows["writer"].systemone) == (True, False)
    assert member_is_systemone(rows["decider"].uuid) and not member_is_systemone(rows["writer"].uuid)
    # Observed at every sync, on existing rows too; a name the provider did
    # not report on keeps what it had.
    sync_model_configs(provider=PROVIDER, available_model_names=["decider", "writer"],
                       default_arguments={}, systemone_by_name={"writer": True})
    db.session.refresh(rows["writer"]), db.session.refresh(rows["decider"])
    assert (rows["decider"].systemone, rows["writer"].systemone) == (True, True)


def test_a_systemone_group_requires_structured_output_and_admits_only_its_kind(models):
    rows, made = models
    group = create_model_group("pp-systemone-test", structured_output_constraint="must_not_have",
                               systemone_constraint="must_have")
    made.append(group)
    # Not a choice: a SystemOne model's output is always structured.
    assert (group.systemone_constraint, group.structured_output_constraint) == ("must_have", "must_have")
    assert group.requires_systemone
    set_model_group_members(group.uuid, [rows["decider"].uuid])
    with pytest.raises(ValueError, match="SystemOne"):
        set_model_group_members(group.uuid, [rows["decider"].uuid, rows["writer"].uuid])


def test_a_group_can_keep_systemone_models_out(models):
    rows, made = models
    group = create_model_group("pp-generative-test", systemone_constraint="must_not_have")
    made.append(group)
    assert group.structured_output_constraint == "dont_care" and not group.requires_systemone
    set_model_group_members(group.uuid, [rows["writer"].uuid])
    with pytest.raises(ValueError, match="SystemOne"):
        set_model_group_members(group.uuid, [rows["decider"].uuid])
    with pytest.raises(ValueError, match="systemone_constraint"):
        create_model_group("pp-bad", systemone_constraint="sometimes")
