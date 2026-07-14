# Supervisor — design

The supervisor is rainbox's core runtime: one OS process (`python main.py`)
runs the Flask webserver and the supervisor loop side by side, and every agent
is a short-lived child OS process the supervisor spawns when that agent has
work and reaps when its inbox is empty. Work flows through a Postgres-backed
queue — the `Inbox`/`Journal` pair — so nothing is lost when a process dies:
the supervisor's job is to spawn, watch, kill, recover, and route.

## Architecture

| Piece | File |
|-------|------|
| Supervisor loop, spawn, watchdog, routing, shutdown | `main.py` |
| Queue operations (`enqueue`, `take_item`, `journal_update`, routing reads) | `db/queue.py` (re-exported from the `db` facade) |
| `Inbox` / `Journal` tables | `db/models.py` |
| Agent child-process entrypoint (`python -m agents --socket-fd N`) | `agents/__main__.py` |
| Agent class hierarchy (`Agent` → `ModelGroupAgent` → `StructuredLLMAgent`) | `agents/base.py` |
| Role registry (`agent_config`), class dispatch (`AGENT_CLASS_PATHS`) | `agents/config.py` |

`main()` (`main.py`) starts the supervisor as a non-daemon thread, then serves
the webapp on `127.0.0.1:5000` (werkzeug `make_server`, threaded) — the two
share one process and one `agent_config`, which is why the app must run via
`python main.py`: the web endpoints only *enqueue*; the supervisor thread is
what makes anything execute. The same loop also hosts the cron scheduler
(`db.cron_tick()` throttled to `CRON_TICK_INTERVAL` = 5 s, self-guarded so a
cron bug cannot take down the thread — see `docs/cron-design.md`).

## The queue: inbox → journal

Two tables (`db/models.py`), deliberately named without a `Queue` prefix — the
package (`db.queue`) already marks the subsystem, and the lifecycle-oriented
pair reads better: **Inbox is work waiting to start, Journal is what happened
to it once taken.**

- **`inbox`** — ephemeral. `enqueue(agent_uuid, payload)` inserts a row
  (payload stored as JSON text). Producers include the chat responder trigger
  (`webapp/chat_api.py` — each `CHAT_RESPONDER_UUIDS` member of a room is
  enqueued when a *human* posts), the conversation manager endpoints
  (`webapp/conversation_api.py`), cron `command` fires (`db/cron.py`), the
  kanban Run button, and the supervisor's own routing pass.
- **`journal`** — durable. `take_item(agent_uuid)` atomically pops the oldest
  inbox row for the agent (ordered by the inbox's integer `id`), deletes it,
  and opens a `Journal` row in `processing` carrying the original payload and
  `enqueued_at`. The agent lifecycle then settles it to `completed` or
  `failed` via `journal_update`, with the result JSON attached. `stopped` is
  reserved in `VALID_STATES` and the table's CHECK constraint but is not
  written by the base lifecycle — an operator stop is recorded in the richer
  per-run tables (e.g. `AssistantRun.status`) while the journal row completes
  normally with the stop in its result.

**`journal.id` is a UUID** (the `journal_id` threaded across the codebase —
cron runs, assistant runs, status messages). The `Journal` docstring
(`db/models.py`) carries the reasoning: a UUID is globally unique and
self-describing, so a `journal_id` grep'd from a log file or backup points at
exactly one row without knowing the table, and it crosses process/payload
boundaries without int collisions. The consequence: a random UUID is not
monotonic, so "oldest first" ordering always uses `started_at`, never `id`
(`fetch_unrouted_terminal` in `db/queue.py`). The model is also deliberately
*not* named `AgentRun` — it is the queue-level substrate *beneath* the domain
runs (`AssistantRun`, `ConversationRun`, `CronRun`, `EvalRun`), not a peer;
for richer agents the journal result only points at the fuller trace.

## Spawn-on-demand, reap-when-idle

There are no long-lived workers. Each supervisor pass calls
`agent_uuids_with_work()` (distinct `agent_uuid`s in the inbox) and spawns one
child per `agent_config` role that has pending work and no live process — at
most one process per role. A uuid not in `agent_config` never spawns (the
cron system user exploits this: it authors chat events but is deliberately
unrunnable).

`spawn()` (`main.py`) creates a `socketpair`, marks the child end inheritable,
and `os.posix_spawn`s `python -m agents --socket-fd N` with `PYTHONPATH` set
to the source root. The supervisor then sends exactly one newline-terminated
JSON config message (`{name, uuid, description, next, …}`) down the socket.
The child (`agents/__main__.py`) reads that line, pushes a DB app context, and
dispatches on `agent_kind` (default: the role name) via
`resolve_agent_class`, which imports **only** the selected module — a spawned
persona process never pays the assistant's import bill.

The agent drains its inbox to empty (`Agent.run` in `agents/base.py`), emits a
final `{"status": "idle"}`, and returns — the process exits on its own. The
supervisor sees the socket EOF, `waitpid`s the child, unregisters and closes
the socket, and forgets the agent. New work later means a fresh spawn with a
fresh `setup()`.

**Loop pacing** (`_select_timeout`, `main.py`): the loop blocks in `select()`
on the live agents' sockets — `TICK_TIMEOUT` (1 s) while any agent is alive or
the pass found inbox/routing/cron work, backing off to `IDLE_TICK_TIMEOUT`
(5 s) when fully idle so an at-rest supervisor is not hammering Postgres.

## Status protocol and the heartbeat watchdog

The socket carries one-directional JSONL, agent → supervisor. Statuses:
`processing` (with `journal_id` + payload), `heartbeat` (with `journal_id`,
plus per-agent `_heartbeat_extra()` fields such as the assistant's current
step), `completed`, `failed`, `idle`. The supervisor tracks
`current_journal_id` from these so it always knows what a worker had in
flight; heartbeats are not logged.

**Any message resets the silence timer.** An agent silent for
`HEARTBEAT_TIMEOUT` (60 s) is presumed hung and SIGKILLed. The agent side
keeps healthy-but-slow turns alive: `_handle_with_heartbeat`
(`agents/base.py`) runs `handle()` while a background thread emits a heartbeat
every `HEARTBEAT_INTERVAL` (20 s — well under the 60 s ceiling, so a reasoning
model thinking for minutes with no output survives). A `_send_lock`
serializes socket writes between the heartbeat thread and the main loop.

**Recovery.** When a worker dies with a `current_journal_id` — watchdog kill,
unexpected exit, or supervisor shutdown — `_recover_assistant_journal`
(`main.py`) settles the orphan: if the journal belongs to an assistant run,
`db.recover_interrupted_assistant_run` (`db/assistant.py`) fails the running
step with a reason (including the active model and its timeout), finishes the
run as `killed`, posts a failure notice, and enqueues the run summarizer;
otherwise `fail_journal_if_processing` (`db/queue.py`) marks the journal
`failed` — guarded so it never overwrites a result that already went terminal.
On startup, `_recover_runs_from_previous_supervisor` sweeps runs left active
by a previous supervisor, since no worker of a dead supervisor can be managed
here.

## Routing

The supervisor is the only router; model output never chooses a routing
target. Each pass, `fetch_unrouted_terminal()` (`db/queue.py`) returns
terminal journal rows (`completed` *or* `failed`) with `routed_at` still NULL,
oldest first. For each row (`main.py`):

1. **Dynamic return address first.** A manager (the conversation manager)
   writes `return_to_agent_uuid` into the *inbox payload* of the turn it
   delegates; `Agent.run` copies it into the result as `_routing` — on the
   failure path too. If present, it wins, and it routes failed rows as well,
   so an errored persona turn still wakes its manager.
2. **Static `next` on success only.** Otherwise a `completed` row follows the
   source role's `next` uuid from `agent_config`; a `failed` row without a
   dynamic address routes nowhere.

Routing enqueues an envelope `{from, from_journal_id, state, input, result}`
to the target and stamps `routed_at` (`mark_routed`) — every terminal row is
routed at most once, and the stamp survives restarts.

**Topology.** `agent_config` (`agents/config.py`) declares the static
pipeline: `dreamer → critic → verifier` is the only chain (the no-LLM
end-to-end demo); every other role has `next: None` and is either a terminus
(chat repliers post their reply themselves) or manager-driven via the dynamic
address (personas), which keeps a persona usable standalone.

## Lifecycle

```
enqueue(agent_uuid, payload)          → inbox row
  supervisor pass: uuid has work, no live process
    → posix_spawn + config over socketpair
agent: take_item()                    → inbox row deleted,
                                        journal 'processing'
  handle() runs; heartbeats every 20s; >60s silence → SIGKILL
    → journal 'completed' / 'failed'  (+ '_routing' if the payload had one)
  worker died mid-item               → recovery fails the journal / run
supervisor pass: fetch_unrouted_terminal()
    → dynamic return address, else static 'next' (completed only)
    → enqueue(next, envelope); mark_routed
agent: inbox empty → status 'idle' → process exits → reaped
```

## Agent class hierarchy

Three layers in `agents/base.py`; each owns one concern.

| Class | Owns |
|-------|------|
| `Agent` | The lifecycle: the `take_item` drain loop, `processing → completed/failed` journaling, status emission, the heartbeat thread, preserving the dynamic return address (success *and* failure), and rollback-on-failure (a DB error inside `handle()` leaves the session aborted; without the rollback, journaling the failure would itself raise and strand the item at `processing`). Subclasses override `setup()` and `handle()`; the base `handle()` is an intentional functional stub so a plain `Agent` still runs the pipeline. |
| `ModelGroupAgent` | The model binding: resolves the agent's model group (`agent_model_binding`, set on `/agentmodel`) during `setup()`, and provides `_structured_completion` — one structured call that falls back through the group's members in priority order, consumed as a stream under a wall-clock deadline, capturing per-attempt usage, the reasoning channel, and the raw response (partial on interruption), with `_model_attempt_*` hooks for agents that durably track attempts. Its `handle()` is also a functional stub reporting the resolved candidates — the default dispatch for any role without a specialized class (dreamer/critic/verifier). |
| `StructuredLLMAgent` | The stateless one-call shape: a fixed `system_prompt` + Pydantic `response_model` at construction, a `user_prompt(payload)` hook, exactly one structured call per inbox item, nothing carried between items. |

Specialized agents (assistant, chat agents, router, workspace shell, kanban
worker, …) subclass these and are registered in `AGENT_CLASS_PATHS`
(`agents/config.py`) as `"module:ClassName"` strings, so the table imports
nothing at load. The class-level `uses_model_group` flag opts an agent out of
the `/agentmodel` page when it sources its model elsewhere or runs no LLM.

## Adding a new agent

1. Mint a fixed uuid constant and add the role to `agent_config`
   (`agents/config.py`): `uuid`, `description`, `next` (almost always `None`),
   and capability flags (`requires_structured_output` /
   `requires_function_calling` / `excludes_structured_output` — these gate
   which model groups `/agentmodel` offers; `kanban_authority` /
   `kanban_verified` if it touches boards).
2. Implement the class — usually a `StructuredLLMAgent` or `ModelGroupAgent`
   subclass overriding `handle()` — and register it in `AGENT_CLASS_PATHS`.
   Skipping registration is valid: the role then runs the default
   `ModelGroupAgent`. Many roles can share one class via `agent_kind`
   (persona_egon / persona_benny both run `chat_unstructured`).
3. Bind a model group on `/agentmodel` (unless `uses_model_group = False`).
4. Give it a producer: membership in a chatroom plus `CHAT_RESPONDER_UUIDS`
   (`webapp/chat_api.py`) for chat-triggered agents, an upstream role's
   `next`, or a direct `db.enqueue` call site.

## Shutdown

SIGINT/SIGTERM (`main.py`) sets the stop event and shuts the webserver down
from a helper thread. The supervisor loop exits, SIGKILLs every remaining
agent, runs journal recovery for any in-flight `current_journal_id`
("Supervisor shut down while the assistant run was active."), `waitpid`s the
children, and closes their sockets. `main()` joins the supervisor thread with
a `HEARTBEAT_TIMEOUT + 2 s` timeout and logs a warning if it fails to stop.
The supervisor's start time is exported as `PP3_SUPERVISOR_STARTED` in the
environment so spawned children inherit their uptime.

## Known limitations (verified, deferred)

Documented at their source, kept visible here:

- **Config-read discards the socket remainder** (`agents/__main__.py` header):
  safe only while the supervisor sends exactly one config message and the
  agent never reads the socket again; keep the remainder if either changes.
- **The streaming wall-clock deadline is soft** (`agents/base.py` header): the
  check sits between generator yields, so it cannot fire during a single
  blocked network read — bounded instead by the httpx read timeout and,
  ultimately, the supervisor's heartbeat SIGKILL.
- **One process per role.** Parallelism is across roles, not within one: a
  role's items are drained serially by its single worker.
