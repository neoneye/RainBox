"""Procedural skills: editable markdown "how to" guidance with thin
frontmatter metadata and a candidate -> active lifecycle.

Only `active` skills are eligible for prompt injection (the "candidates are
inert" contract). Facts live in Postgres; skills live in files so they are
diffable and operator-editable.
"""

from skills.loader import Skill, load_skills  # noqa: F401
from skills.retrieval import (  # noqa: F401
    RetrievedSkill,
    build_skill_block,
    format_skill_context,
    retrieve_skills,
)
