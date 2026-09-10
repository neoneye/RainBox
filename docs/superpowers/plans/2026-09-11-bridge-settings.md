# Bridge Settings Implementation Plan

**Status:** implemented on the `bridge-settings` branch (2026-09-11; tasks 1–6 done), against
`docs/superpowers/specs/2026-09-09-bridge-settings-design.md`, migration
steps 1–3 (tables/API/UI, launcher extension, Discord DB mode + import).
Telegram DB mode (step 4) and Zulip (step 5) are follow-ups by the design's
own sequencing.

**Goal:** Bridge connectors, folders, and bindings live in Postgres and are
edited on a `/bridges` page; the core pushes each connector as a dynamic
launcher entry and announces config changes on the chat SSE stream; the
Discord bridge runs in DB mode under the launcher with per-binding policy
and state, without polling for configuration.

**Architecture:** Three tables with `RESTRICT` foreign keys for ownership
and plain uuid placement columns under a version token and a connector row
lock. `db/bridges.py` owns validation, the policy resolver, the tree
save/load, delete previews, the config snapshot, the `bridge_config`
NOTIFY, and the launcher desired entries; `webapp/bridges_api.py` and
`webapp/bridges_views.py` + `static/bridges.js` (ported from `/git`) expose
them. The launcher gains dynamic kinds. The Discord bridge gains a
`BRIDGE_CONNECTOR` mode with an event-driven config fetch.

## Tasks

1. **Data model + db layer.** `db/models.py` (`BridgeConnector`,
   `BridgeFolder`, `BridgeBinding`), `db/bridges.py` (adapter registry per
   platform: address validation + canonical key + policy defaults; policy
   resolver; effective enabled; tree version/load/save under connector
   locks; create/delete with 409 blockers; config snapshot in REPEATABLE
   READ; room/chat-folder delete blockers; `bridge_config` notify; restart
   nonce; launcher desired entries), re-exported from `db`;
   `db/test_bridges.py`.
2. **Core API + integration.** `webapp/bridges_api.py` (six-endpoint tree
   shape + connector/binding CRUD + config endpoint + restart), chat room
   and folder delete previews and handlers (blockers, 409), `services.bridges.autostart`
   setting, `services/registry.desired_snapshot` with bridge entries and a
   push on connector-level writes, `POST /services/api/restart/bridge:<uuid>`;
   `webapp/test_bridges_api.py`.
3. **Launcher extension.** `services/definitions.py` dynamic kinds
   (`discord_bridge`, `telegram_bridge`) with a state-file variable;
   `main.py`: dynamic keys in `validate_desired`, credential resolution and
   `credential missing`, state-file variable, label prefix, dynamic keys in
   status; `services/registry.LauncherStatus.view` carries dynamic keys;
   tests in `test_main.py` and `webapp/test_services_api.py`.
4. **`/bridges` page.** `webapp/bridges_views.py` + `static/bridges.js`
   ported rule-for-rule from `/git` (tree, kebabs, modals, drag-drop, deep
   link), with detail panes for connector (fields, enabled, launch mode,
   launcher status, Restart, launch command), folder (enabled, policy), and
   binding (room, address, enabled, policy with effective values); marker
   tests in `webapp/test_bridges_views.py`; a live check via `tools.serve_ui`.
5. **Discord DB mode.** `discord_service/bridge.py`: `BRIDGE_CONNECTOR` mode
   with config fetched at SSE connect and on `bridge_config` events,
   multi-binding routing, policy keys, per-binding state under a lock, exit
   codes 2/3, removal cleanup, freshness as a fact; legacy env mode kept;
   `discord_service/import_legacy.py`; tests.
6. **Docs + integration.** README rows, design status, full suites, sweep,
   push.
