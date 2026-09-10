"""Chat-bridge platform adapters: what a remote address looks like per
platform, which policy keys exist with what defaults and types, and how a
policy resolves through connector -> folders -> binding.

Standard library only, like `services.definitions`: the core imports it to
validate rows and resolve config snapshots. A bridge process does not import
it — the config endpoint's wire schema (`schema_version`) is the contract,
and each bridge validates what it receives itself. Nothing here touches a
database or a network.

Design: docs/superpowers/specs/2026-09-09-bridge-settings-design.md
("Platform addresses", "Policy resolution and enabled gates").
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

_SNOWFLAKE = re.compile(r"^[0-9]{1,20}$")
_SIGNED_INT = re.compile(r"^-?[0-9]{1,20}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Env names a connector's token_env may not use: they are deployment facts
# the launcher or the bridge itself sets, and a token there would collide.
RESERVED_ENV_NAMES: frozenset[str] = frozenset({
    "BRIDGE_CONNECTOR", "RAINBOX_URL", "DISCORD_STATE_FILE", "TELEGRAM_STATE_FILE",
    "ZULIP_STATE_FILE", "PATH", "HOME", "PYTHONPATH", "DATABASE_URL",
})

ROW_KINDS: tuple[str, ...] = ("message", "notice", "progress")
DIRECTIONS: tuple[str, ...] = ("both", "in", "out")
LAUNCH_MODES: tuple[str, ...] = ("launcher", "manual")


class AdapterError(ValueError):
    """A platform, address, or policy that the adapter registry rejects. The
    message is safe to show an operator."""


@dataclass(frozen=True)
class PolicyKey:
    name: str
    type: str                      # "list_str" | "bool" | "number" | "choice"
    default: Any
    supported: bool = True         # False: the adapter rejects a value for it
    choices: tuple[str, ...] = ()  # for "choice"; for list_str, allowed members (empty = any)
    minimum: float | None = None
    maximum: float | None = None
    description: str = ""


@dataclass(frozen=True)
class AddressField:
    name: str
    kind: str        # "snowflake" | "signed_int" | "text"
    required: bool = True
    routing: bool = True   # part of the canonical address key


@dataclass(frozen=True)
class Adapter:
    platform: str
    kind: str                       # launcher service kind, e.g. "discord_bridge"
    label: str
    state_file_env: str
    available: bool                 # has a bridge implementation in this repo
    requires_base_url: bool
    requires_identity: bool
    address_fields: tuple[AddressField, ...]
    policy_keys: dict[str, PolicyKey] = field(default_factory=dict)

    # --- addresses ---------------------------------------------------------------

    def validate_address(self, address: Any) -> dict[str, str]:
        """Normalize and validate a remote address; unknown keys are rejected,
        ids are canonical decimal strings. Returns the normalized dict."""
        if not isinstance(address, dict):
            raise AdapterError("address must be a JSON object")
        known = {f.name: f for f in self.address_fields}
        unknown = set(address) - set(known)
        if unknown:
            raise AdapterError(f"unknown address field(s): {', '.join(sorted(unknown))}")
        out: dict[str, str] = {}
        for spec in self.address_fields:
            raw = address.get(spec.name)
            if raw is None or raw == "":
                if spec.required:
                    raise AdapterError(f"address field {spec.name!r} is required")
                continue
            if isinstance(raw, bool) or not isinstance(raw, (str, int)):
                raise AdapterError(f"address field {spec.name!r} must be a string")
            text = str(raw).strip()
            if spec.kind == "snowflake" and not _SNOWFLAKE.match(text):
                raise AdapterError(f"address field {spec.name!r} must be a numeric id")
            if spec.kind == "signed_int" and not _SIGNED_INT.match(text):
                raise AdapterError(f"address field {spec.name!r} must be an integer id")
            if spec.kind == "text" and not text:
                raise AdapterError(f"address field {spec.name!r} must not be empty")
            out[spec.name] = text
        return out

    def address_key(self, address: dict[str, str]) -> str:
        """Canonical text for the uniqueness constraint: routing fields only,
        in declaration order."""
        parts = [f"{f.name}={address[f.name]}" for f in self.address_fields
                 if f.routing and f.name in address]
        return "|".join(parts)

    # --- policies ----------------------------------------------------------------

    def validate_policy(self, policy: Any) -> dict[str, Any]:
        """A policy object as stored on a connector, folder, or binding: known
        keys only, each either JSON null (inherit) or a value of the declared
        type; keys the adapter does not support are rejected outright."""
        if policy is None:
            return {}
        if not isinstance(policy, dict):
            raise AdapterError("policy must be a JSON object")
        out: dict[str, Any] = {}
        for name, value in policy.items():
            spec = self.policy_keys.get(name)
            if spec is None:
                raise AdapterError(f"unknown policy key {name!r}")
            if not spec.supported:
                raise AdapterError(f"policy key {name!r} is not supported by {self.platform}")
            if value is None:
                out[name] = None
                continue
            out[name] = self._check_value(spec, value)
        return out

    @staticmethod
    def _check_value(spec: PolicyKey, value: Any) -> Any:
        if spec.type == "bool":
            if not isinstance(value, bool):
                raise AdapterError(f"policy {spec.name} must be true or false")
            return value
        if spec.type == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise AdapterError(f"policy {spec.name} must be a number")
            if value != value or value in (float("inf"), float("-inf")):
                raise AdapterError(f"policy {spec.name} must be finite")
            if spec.minimum is not None and value < spec.minimum:
                raise AdapterError(f"policy {spec.name} must be >= {spec.minimum}")
            if spec.maximum is not None and value > spec.maximum:
                raise AdapterError(f"policy {spec.name} must be <= {spec.maximum}")
            return value
        if spec.type == "choice":
            if not isinstance(value, str) or value not in spec.choices:
                raise AdapterError(f"policy {spec.name} must be one of {', '.join(spec.choices)}")
            return value
        if spec.type == "list_str":
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise AdapterError(f"policy {spec.name} must be a list of strings")
            if spec.choices:
                bad = [v for v in value if v not in spec.choices]
                if bad:
                    raise AdapterError(f"policy {spec.name}: unsupported value(s) {', '.join(bad)}")
            if len(set(value)) != len(value):
                raise AdapterError(f"policy {spec.name} must not repeat values")
            return list(value)
        raise AdapterError(f"policy {spec.name}: unknown type")  # pragma: no cover

    def resolve_policy(self, layers: list[tuple[str, dict[str, Any] | None]]) -> tuple[dict[str, Any], dict[str, str]]:
        """Nearest-wins per key over `layers` ordered root -> leaf, each
        (level_name, policy). A missing key or null inherits; a present
        non-null value replaces (lists replace lists). Returns (effective,
        sources) where sources maps each key to the level that supplied it
        ("default" when none did)."""
        effective: dict[str, Any] = {}
        sources: dict[str, str] = {}
        for name, spec in self.policy_keys.items():
            effective[name] = spec.default
            sources[name] = "default"
        for level, policy in layers:
            if not policy:
                continue
            for name, value in policy.items():
                if name in self.policy_keys and value is not None:
                    effective[name] = value
                    sources[name] = level
        return effective, sources


def _common_policy(forward_default: list[str], poll_supported: bool, mirror_supported: bool) -> dict[str, PolicyKey]:
    return {
        "allowed_senders": PolicyKey(
            "allowed_senders", "list_str", [],
            description="Platform user ids allowed to post inbound; empty denies all."),
        "forward_kinds": PolicyKey(
            "forward_kinds", "list_str", forward_default, choices=ROW_KINDS,
            description="Agent row kinds forwarded outbound."),
        "poll_seconds": PolicyKey(
            "poll_seconds", "number", 2, supported=poll_supported, minimum=0.5, maximum=300,
            description="Inbound channel poll interval."),
        "mirror_progress": PolicyKey(
            "mirror_progress", "bool", True, supported=mirror_supported,
            description="Edit one remote bubble per progress row instead of posting each update."),
        "direction": PolicyKey(
            "direction", "choice", "both", choices=DIRECTIONS,
            description="Traffic direction relative to rainbox."),
    }


ADAPTERS: dict[str, Adapter] = {
    "discord": Adapter(
        platform="discord", kind="discord_bridge", label="Discord",
        state_file_env="DISCORD_STATE_FILE", available=True,
        requires_base_url=False, requires_identity=False,
        address_fields=(AddressField("channel_id", "snowflake"),
                        AddressField("guild_id", "snowflake", required=False, routing=False)),
        policy_keys=_common_policy(["message", "notice", "progress"], True, True),
    ),
    "telegram": Adapter(
        platform="telegram", kind="telegram_bridge", label="Telegram",
        # The Telegram bridge still reads only its legacy environment; until
        # it gains a connector mode a connector for it could never start.
        state_file_env="TELEGRAM_STATE_FILE", available=False,
        requires_base_url=False, requires_identity=False,
        address_fields=(AddressField("chat_id", "signed_int"),),
        policy_keys=_common_policy(["message"], False, False),
    ),
    "zulip": Adapter(
        platform="zulip", kind="zulip_bridge", label="Zulip",
        state_file_env="ZULIP_STATE_FILE", available=False,
        requires_base_url=True, requires_identity=True,
        address_fields=(AddressField("stream_id", "snowflake"), AddressField("topic", "text")),
        policy_keys=_common_policy(["message", "notice", "progress"], False, True),
    ),
}


def adapter_for(platform: Any) -> Adapter:
    if not isinstance(platform, str) or platform not in ADAPTERS:
        raise AdapterError(f"unknown platform {platform!r}")
    return ADAPTERS[platform]


def validate_token_env(name: Any) -> str:
    if not isinstance(name, str) or not _ENV_NAME.match(name):
        raise AdapterError("token_env must be an environment variable name")
    if name in RESERVED_ENV_NAMES:
        raise AdapterError(f"token_env may not be the reserved name {name}")
    return name


def validate_base_url(url: Any) -> str:
    """An HTTPS realm URL with no userinfo, query, or fragment — it decides
    where a credential is sent."""
    if not isinstance(url, str) or not url.strip():
        raise AdapterError("base_url is required for this platform")
    url = url.strip()
    m = re.match(r"^https://([A-Za-z0-9.-]+)(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?$", url)
    if not m:
        raise AdapterError("base_url must be an https:// URL without userinfo, query, or fragment")
    return url


def sanitize_label(name: Any) -> str:
    """One printable line for a launcher log prefix; never shell syntax."""
    text = "".join(ch for ch in str(name or "") if ch.isprintable()).strip()
    return text[:60] or "bridge"


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
