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
class EnvVar:
    """One operator-settable, non-secret environment variable of a service,
    typed so an invalid edit is refused BEFORE it restarts anything: a child
    that raises while parsing its environment would otherwise burn through
    its crash budget on a typo."""
    name: str
    type: str = "string"                    # "string" | "int"
    positive: bool = False                  # int: must be >= 1
    choices: tuple[str, ...] | None = None  # string: the only accepted values
    description: str = ""


@dataclass(frozen=True)
class ServiceKind:
    kind: str
    directory: str                 # relative to source/
    argv: tuple[str, ...]          # relative to `directory`; argv[0] = its venv python
    bind: str                      # what the entrypoint listens on (display only)
    env_keys: tuple[str, ...]      # non-secret env vars the operator may set
    description: str
    core_url_env: str | None = None  # the core's discovery variable, if any
    env_specs: tuple[EnvVar, ...] = ()  # types for env_keys; a key without one is a free string
    # A dynamic kind is instantiated per database row (chat-bridge connectors:
    # key `bridge:<uuid>`), never as a static entry, and carries a credential
    # by variable NAME and a per-instance state file through `state_file_env`.
    dynamic: bool = False
    state_file_env: str | None = None

    def env_spec(self, name: str) -> EnvVar:
        for spec in self.env_specs:
            if spec.name == name:
                return spec
        return EnvVar(name)

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
        env_specs=(
            EnvVar("WHISPER_MODEL", description="faster-whisper model name, e.g. small.en, medium.en, large-v3-turbo."),
            EnvVar("WHISPER_COMPUTE_TYPE", choices=(
                "default", "auto", "int8", "int8_float32", "int8_float16", "int8_bfloat16",
                "int16", "float16", "bfloat16", "float32"),
                description="CTranslate2 compute type."),
            EnvVar("WHISPER_CPU_THREADS", type="int", positive=True,
                   description="CPU threads for inference."),
        ),
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
        env_specs=(
            EnvVar("RERANKER_MAX_LENGTH", type="int", positive=True, description="Token cap per query/passage pair."),
            EnvVar("RERANKER_BATCH_SIZE", type="int", positive=True, description="Pairs scored per forward pass."),
            EnvVar("RERANKER_DEVICE", choices=("auto", "cpu", "mps", "cuda"),
                   description="torch device; auto picks the best available."),
        ),
    ),
}

DYNAMIC_SERVICES: dict[str, ServiceKind] = {
    "discord_bridge": ServiceKind(
        kind="discord_bridge", directory="discord_service",
        argv=("venv/bin/python", "bridge.py"), bind="outbound only",
        env_keys=("RAINBOX_URL", "BRIDGE_CONNECTOR"),
        description="Discord <-> chatroom bridge, one process per connector.",
        dynamic=True, state_file_env="DISCORD_STATE_FILE",
    ),
    "telegram_bridge": ServiceKind(
        kind="telegram_bridge", directory="telegram_service",
        argv=("venv/bin/python", "bridge.py"), bind="outbound only",
        env_keys=("RAINBOX_URL", "BRIDGE_CONNECTOR"),
        description="Telegram <-> chatroom bridge, one process per connector.",
        dynamic=True, state_file_env="TELEGRAM_STATE_FILE",
    ),
}

ALL_KINDS: dict[str, ServiceKind] = {**STATIC_SERVICES, **DYNAMIC_SERVICES}

CORE_KEY: str = "core"
BRIDGE_KEY_PREFIX: str = "bridge:"


def enabled_setting_key(service_key: str) -> str:
    return f"services.{service_key}.enabled"


def nonce_setting_key(service_key: str) -> str:
    return f"services.{service_key}.restart_nonce"


def env_setting_key(service_key: str, var: str) -> str:
    return f"services.{service_key}.env.{var}"
