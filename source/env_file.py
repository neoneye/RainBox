"""Repo-root `.env` loading.

Secrets that don't belong in the database or in the repo (currently the
OpenRouter API key) live in a gitignored `.env` at the repo root, next to
`source/`. `.env.example` documents which keys it may hold.

`load_env_file()` is called at import of `providers/__init__.py` — the one
choke point every process that builds an LLM passes through (the web app,
`main.py`, the benchmark runners, and the killable `llm/models_test_worker.py`
subprocess all import `providers`, directly or through `llm`). Provider
configuration already reads the environment (`OLLAMA_BASE_URL`, `JAN_BASE_URL`,
`LMS`), so the file is a fallback layer under that, not a new mechanism.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# source/env_file.py -> source/ -> repo root
REPO_ROOT: Path = Path(__file__).resolve().parent.parent
ENV_PATH: Path = REPO_ROOT / ".env"

_loaded: bool = False


def load_env_file(path: Path | None = None) -> Path | None:
    """Load the repo-root `.env` into os.environ, once per process. Returns the
    path that was loaded, or None when the file doesn't exist.

    Variables already present in the environment always win: `.env` fills gaps,
    so `OPENROUTER_API_KEY=… python main.py` still overrides the file, and a
    test that sets the variable can't be clobbered by the operator's real key.
    """
    global _loaded
    env_path = path or ENV_PATH
    if _loaded and path is None:
        return env_path if env_path.exists() else None
    if not env_path.exists():
        _loaded = True
        return None
    from dotenv import load_dotenv

    load_dotenv(env_path, override=False)
    _loaded = True
    logger.debug("loaded environment file %s", env_path)
    return env_path
