"""The record of the decision models' calls (`llm/systemone.py`)."""
from typing import Any

from db.models import DecisionCall, db

__all__ = ["list_decision_calls", "record_decision_call"]


def record_decision_call(**values: Any) -> DecisionCall:
    call = DecisionCall(**values)
    db.session.add(call)
    db.session.commit()
    return call


def list_decision_calls(job_uuid: Any) -> list[DecisionCall]:
    return (db.session.query(DecisionCall).filter_by(job_uuid=job_uuid)
            .order_by(DecisionCall.created_at.asc()).all())
