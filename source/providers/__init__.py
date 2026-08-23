"""Provider registry package — re-exports the public surface."""

from env_file import load_env_file

# Providers read their configuration from the environment (OLLAMA_BASE_URL,
# OPENROUTER_API_KEY, LMS, …), so the repo-root .env is loaded here — the one
# import every process that builds an LLM performs, including the killable
# /model test-worker subprocess.
load_env_file()

from .base import (  # noqa: E402  .env must be in place before providers read it
    PREFERRED_PROVIDER_ID,
    PROVIDER_ORDER,
    Provider,
    ProviderId,
    provider_sort_key,
)
from .registry import all_providers, get, request_api_key  # noqa: E402

__all__ = [
    "PREFERRED_PROVIDER_ID",
    "PROVIDER_ORDER",
    "Provider",
    "ProviderId",
    "all_providers",
    "get",
    "provider_sort_key",
    "request_api_key",
]
