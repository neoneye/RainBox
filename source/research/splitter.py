"""Stage 2: research plan -> 3-8 independent subtasks (structured).

Ids (S1, S2, ...) and the max_subtasks cap are assigned in Python — the
model only ever produces titles and descriptions."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field

from research import prompts
from research.caller import Caller


class SubtaskModel(BaseModel):
    title: str = Field(description="Short descriptive title of the subtask.")
    description: str = Field(
        description="Detailed instructions for researching this slice of the plan."
    )


class SubtaskListModel(BaseModel):
    subtasks: list[SubtaskModel] = Field(
        description="Non-overlapping subtasks that together cover the whole plan."
    )


@dataclass
class Subtask:
    id: str
    title: str
    description: str


def split_plan(caller: Caller, plan: str, max_subtasks: int) -> list[Subtask]:
    result = caller.structured(prompts.SPLITTER_SYSTEM, plan, SubtaskListModel)
    assert isinstance(result, SubtaskListModel)
    rows = [
        row
        for row in result.subtasks
        if row.title.strip() and row.description.strip()
    ][:max_subtasks]
    if not rows:
        raise RuntimeError("splitter produced no subtasks")
    return [
        Subtask(id=f"S{i}", title=row.title.strip(), description=row.description.strip())
        for i, row in enumerate(rows, start=1)
    ]
