"""The data-only service catalogue. Standard library only — the launcher
imports this module, and the launcher imports nothing from the application.

A *kind* is one runnable side service: its directory under `source/`, the argv
that runs it (relative to that directory; `argv[0]` is the service's OWN venv
interpreter), the address it binds (display only — the port is hard-coded in
each entrypoint), and the non-secret environment variables the operator may
set for it. Static services have key == kind; the later bridge extension adds
dynamic keys of the form `bridge:<uuid>` for a registered bridge kind.

HTTP never carries paths, argv, or executables: the core's desired-state
snapshot names a kind and the launcher resolves it here, so a compromised or
confused core cannot make the launcher run something new.
"""
from dataclasses import dataclass

SCHEMA_VERSION: int = 1

# Environment a service child always gets (when present in the launcher's own
# environment). Everything else must be declared by its kind.
BASELINE_ENV_KEYS: tuple[str, ...] = ("PATH", "HOME", "LANG", "TMPDIR")
BASELINE_ENV_PREFIXES: tuple[str, ...] = ("LC_",)

# Exit-code convention services may implement (docs: "Exit codes").
EXIT_CONFIG_REJECTED: int = 2   # deterministic configuration/credential failure
EXIT_LOCK_HELD: int = 3         # an ownership lock is held by another process


@dataclass(frozen=True)
class ServiceKind:
    kind: str
    directory: str                 # relative to source/
    argv: tuple[str, ...]          # relative to `directory`; argv[0] = its venv python
    bind: str                      # what the entrypoint listens on (display only)
    env_keys: tuple[str, ...]      # non-secret env vars the operator may set
    description: str
    core_url_env: str | None = None  # the core's discovery variable, if any

    @property
    def key(self) -> str:
        return self.kind


STATIC_SERVICES: dict[str, ServiceKind] = {
    "voice_tts_kokoro": ServiceKind(
        kind="voice_tts_kokoro",
        directory="voice_tts_kokoro",
        argv=("venv/bin/python", "server.py"),
        bind="127.0.0.1:5005",
        env_keys=(),
        description="Kokoro text-to-speech (torch) for /voice pages.",
        core_url_env="KOKORO_TTS_URL",
    ),
    "voice_stt_whisper": ServiceKind(
        kind="voice_stt_whisper",
        directory="voice_stt_whisper",
        argv=("venv/bin/python", "server.py"),
        bind="127.0.0.1:5006",
        env_keys=("WHISPER_MODEL", "WHISPER_COMPUTE_TYPE", "WHISPER_CPU_THREADS"),
        description="faster-whisper speech-to-text for /voice pages.",
        core_url_env="WHISPER_STT_URL",
    ),
    "voice_tts_dotstts": ServiceKind(
        kind="voice_tts_dotstts",
        directory="voice_tts_dotstts",
        argv=("venv/bin/python", "server.py"),
        bind="127.0.0.1:5007",
        env_keys=(),
        description="dots.tts voice-clone text-to-speech.",
        core_url_env="DOTS_TTS_URL",
    ),
    "reranker": ServiceKind(
        kind="reranker",
        directory="reranker",
        argv=("venv/bin/python", "server.py"),
        bind="127.0.0.1:5008",
        env_keys=("RERANKER_MAX_LENGTH", "RERANKER_BATCH_SIZE", "RERANKER_DEVICE"),
        description="Cross-encoder reranker for memory recall.",
    ),
}

CORE_KEY: str = "core"


def enabled_setting_key(service_key: str) -> str:
    return f"services.{service_key}.enabled"


def nonce_setting_key(service_key: str) -> str:
    return f"services.{service_key}.restart_nonce"


def env_setting_key(service_key: str, var: str) -> str:
    return f"services.{service_key}.env.{var}"
