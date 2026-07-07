"""Deep research: query -> cited markdown report.

Deterministic pipeline (plan -> split -> research subtasks -> synthesize)
over pluggable web search providers and local LLMs. Public seam:
`run_deep_research(query, config, progress_cb)`. See
source/docs/deep-research.md.

Lazy re-exports keep `import research` cheap (pipeline pulls db + llm).
"""

from typing import Any


def __getattr__(name: str) -> Any:
    if name == "run_deep_research":
        from research.pipeline import run_deep_research

        return run_deep_research
    if name == "ResearchConfig":
        from research.config import ResearchConfig

        return ResearchConfig
    raise AttributeError(name)
