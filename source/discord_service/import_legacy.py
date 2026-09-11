"""One-time import of a legacy env-mode Discord bridge into a connector row.

Run from discord_service/ with the SAME environment the legacy bridge used
(minus the token, which this tool never reads):

    venv/bin/python import_legacy.py --name "Main Bot" --state-dir /path/to/var/services

What it does, through the core's HTTP API only (no database access):

1. Resolves `DISCORD_ROOM_NAME` (default `discord`) to exactly one chatroom;
   an ambiguous or missing name is an error, never a guess.
2. Creates a DISABLED connector (platform discord, `token_env` defaulting to
   `DISCORD_BOT_TOKEN` — the variable NAME only) and a DISABLED binding for
   `DISCORD_CHANNEL_ID`, copying the nonsecret settings (`allowed_senders`
   from `DISCORD_ALLOWED_USER_IDS`, `poll_seconds` from `DISCORD_POLL_SECONDS`)
   as the connector's policy.
3. If the legacy state file (`DISCORD_STATE_FILE`, default `./state.json`)
   exists, writes a schema-2 state file `<state-dir>/bridge-<connector>.json`
   with the legacy cursors and progress map wrapped under the new binding's
   uuid, so nothing is replayed and live bubbles stay tracked. The legacy
   file is left untouched for rollback. Refuses to overwrite an existing
   target file.

Then: stop the legacy process, verify the connector on /bridges, enable the
binding and the connector, and let the launcher start it (or run the shown
manual command). Never run both modes for the same bot at once.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import requests

STATE_SCHEMA_VERSION = 2


def _die(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(2)


def resolve_room(base: str, name: str, session: Any) -> dict[str, Any]:
    resp = session.get(f"{base}/chat/api/rooms", timeout=10)
    resp.raise_for_status()
    matches = [r for r in resp.json() if r.get("name") == name]
    if not matches:
        _die(f"no chatroom named {name!r} at {base}")
    if len(matches) > 1:
        _die(f"{len(matches)} chatrooms are named {name!r}; rename all but one first")
    return matches[0]


def legacy_settings(env: dict[str, str]) -> dict[str, Any]:
    channel = (env.get("DISCORD_CHANNEL_ID") or "").strip()
    if not channel.isdigit():
        _die("DISCORD_CHANNEL_ID must be set to the legacy channel's numeric id")
    senders = [p.strip() for p in (env.get("DISCORD_ALLOWED_USER_IDS") or "").split(",") if p.strip()]
    if not all(s.isdigit() for s in senders):
        _die("DISCORD_ALLOWED_USER_IDS must be comma-separated numeric ids")
    try:
        poll = float((env.get("DISCORD_POLL_SECONDS") or "2").strip())
    except ValueError:
        _die("DISCORD_POLL_SECONDS must be a number")
        raise
    return {"channel_id": channel, "allowed_senders": senders, "poll_seconds": poll,
            "room_name": (env.get("DISCORD_ROOM_NAME") or "discord").strip(),
            "state_file": Path(env.get("DISCORD_STATE_FILE") or "state.json"),
            "rainbox_url": (env.get("RAINBOX_URL") or "http://127.0.0.1:5000").strip().rstrip("/")}


def wrap_state(legacy: dict[str, Any], connector_uuid: str, binding_uuid: str,
               room_uuid: str, channel_id: str) -> dict[str, Any]:
    """The legacy flat state under the new identities; remote_identity is
    learned on the first connector-mode start."""
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "connector_uuid": connector_uuid,
        "platform": "discord",
        "remote_identity": None,
        "bindings": {
            binding_uuid: {
                "room_uuid": room_uuid,
                "address": {"channel_id": channel_id},
                "address_key": f"channel_id={channel_id}",
                "discord_after": str(legacy.get("discord_after", "0")),
                "room_cursor": int(legacy.get("room_cursor", 0)),
                "progress_messages": dict(legacy.get("progress_messages") or {}),
            }
        },
    }


def run(argv: list[str] | None = None, env: dict[str, str] | None = None, session: Any = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", required=True, help="connector name shown on /bridges")
    parser.add_argument("--token-env", default="DISCORD_BOT_TOKEN",
                        help="the credential variable NAME the connector will read (default DISCORD_BOT_TOKEN)")
    parser.add_argument("--state-dir", required=True, type=Path,
                        help="where connector-mode state files live (the launcher's state dir for a supervised run)")
    args = parser.parse_args(argv)
    env = dict(os.environ if env is None else env)
    session = session or requests.Session()
    legacy = legacy_settings(env)
    base = legacy["rainbox_url"]

    room = resolve_room(base, legacy["room_name"], session)
    conn_resp = session.post(f"{base}/bridges/api/connectors", json={
        "name": args.name, "platform": "discord", "token_env": args.token_env,
        "policy": {"allowed_senders": legacy["allowed_senders"], "poll_seconds": legacy["poll_seconds"]},
    }, timeout=10)
    if conn_resp.status_code != 201:
        _die(f"creating the connector failed: {conn_resp.status_code} {conn_resp.text[:300]}")
    connector = conn_resp.json()["connector"]
    bind_resp = session.post(f"{base}/bridges/api/bindings", json={
        "connectorId": connector["uuid"], "roomUuid": room["uuid"],
        "address": {"channel_id": legacy["channel_id"]},
    }, timeout=10)
    if bind_resp.status_code != 201:
        _die(f"creating the binding failed: {bind_resp.status_code} {bind_resp.text[:300]}")
    binding = bind_resp.json()["binding"]

    target = args.state_dir / f"bridge-{connector['uuid']}.json"
    wrote_state = False
    if legacy["state_file"].exists():
        if target.exists():
            _die(f"{target} already exists; refusing to overwrite it")
        try:
            old = json.loads(legacy["state_file"].read_text())
        except json.JSONDecodeError as exc:
            _die(f"legacy state file {legacy['state_file']} is not valid JSON ({exc})")
            raise
        args.state_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(wrap_state(old, connector["uuid"], binding["uuid"],
                                                str(room["uuid"]), legacy["channel_id"])))
        wrote_state = True

    print(f"connector {connector['uuid']} ({args.name}) created, disabled; credential variable: {args.token_env}")
    print(f"binding {binding['uuid']}: room {room['name']!r} ({room['uuid']}) <-> channel {legacy['channel_id']}, disabled")
    if wrote_state:
        print(f"state wrapped into {target} (legacy {legacy['state_file']} kept for rollback)")
    else:
        print(f"no legacy state at {legacy['state_file']}: the first start begins at newest (no replay)")
    print("next: stop the legacy process, check /bridges, enable the binding and the connector, "
          "paste the token on the connector pane (it is stored sealed), then let the launcher start it")
    return {"connector": connector, "binding": binding, "state_file": str(target) if wrote_state else None}


if __name__ == "__main__":
    run()
