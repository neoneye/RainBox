"""User profile block: a compact, query-independent digest of the operator's
active self-model (preferences, project decisions, stable facts), injected into
the assistant prompt like the skills block.

Read-only: it surfaces existing *active* memory claims with provenance; it never
creates or infers claims (derivation is the optional Phase 3.5 deriver). Named
``user_profile`` rather than ``profile`` to avoid shadowing the stdlib profiler.
"""

from user_profile.context import (
    ProfileContext,
    current_profile_context,
)
from user_profile.formatting import (
    MAX_FORMATTING_GUIDE_CHARS,
    build_formatting_guide,
    format_formatting_guide,
)
from user_profile.identity import (
    build_identity_block,
    current_profile,
    format_identity_block,
)
from user_profile.retrieval import (
    MAX_PROFILE_BLOCK_CHARS,
    MAX_PROFILE_FACTS,
    RetrievedProfileFact,
    build_profile_block,
    format_profile_context,
    select_profile_facts,
)

__all__ = [
    "MAX_FORMATTING_GUIDE_CHARS",
    "ProfileContext",
    "current_profile_context",
    "MAX_PROFILE_BLOCK_CHARS",
    "MAX_PROFILE_FACTS",
    "RetrievedProfileFact",
    "build_formatting_guide",
    "build_identity_block",
    "build_profile_block",
    "current_profile",
    "format_formatting_guide",
    "format_identity_block",
    "format_profile_context",
    "select_profile_facts",
]
