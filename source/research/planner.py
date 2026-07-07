"""Stage 1: query -> research plan (plain text)."""

from __future__ import annotations

from research import prompts
from research.caller import Caller


def generate_plan(caller: Caller, query: str) -> str:
    plan = caller.plain(prompts.PLANNER_SYSTEM, query).strip()
    if not plan:
        raise RuntimeError("planner produced an empty plan")
    return plan
