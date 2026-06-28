"""The /assistant step deep-link builder shared by the chat proposal card and
the cron provenance row."""

from uuid import UUID

from db.assistant import assistant_step_path


def test_assistant_step_path_format():
    run = UUID("11111111-1111-1111-1111-111111111111")
    step = UUID("22222222-2222-2222-2222-222222222222")
    assert assistant_step_path(run, step) == (
        "/assistant?id=11111111-1111-1111-1111-111111111111"
        "#step-22222222-2222-2222-2222-222222222222"
    )
