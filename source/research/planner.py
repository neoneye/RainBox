"""Stage 1: query -> research plan (plain text)."""

from __future__ import annotations

from research import prompts
from research.caller import Caller


def generate_plan(caller: Caller, query: str, scope_block: str = "") -> str:
    user_prompt = f"{query}\n\n{scope_block}" if scope_block else query
    plan = caller.plain(prompts.PLANNER_SYSTEM, user_prompt).strip()
    if not plan:
        raise RuntimeError("planner produced an empty plan")
    return plan
