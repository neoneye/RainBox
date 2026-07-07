# Conversations — design

**Status: built and running (Phase 0).** Bounded persona-to-persona
conversations: two (or more) LLM personas talk to each other in a normal chat
room, scheduled by a manager agent, with hard bounds, operator controls, and
restart-safe turn accounting. Start/stop/resume from the `/conversations`
page.

The separation of concerns: the **manager** owns *who speaks when and when to
stop* (mechanical, no LLM call); the **personas** own *what to say* (ordinary
chat agents with a file-backed system prompt); the **transcript** stays in
`chat_message` like any other room. The only mutable runtime state the
feature adds is one `conversation_run` row per conversation.

## Personas (data, not code)

Personas are file-backed (`agent_profiles/`):

- `personas.jsonl` — one record per persona: `persona_id`, `slug`, `name`,
  `description`, prompt file reference, `agent_kind`
  (`chat_unstructured` in Phase 0), `agent_role` / `agent_uuid` (the runnable
  identity in `agent_config`), `chat_user_uuid` (the visible speaker; v1 ==
  `agent_uuid`).
- `prompts/<slug>.system.md` — the system prompt body. Its SHA-256 is stamped
  on each turn for provenance (`prompt_sha256`).
- `conversations/<slug>.json` — conversation templates: participant slugs +
  turn policy defaults.

`agents/persona.py` loads and caches them (`load_personas`, lru-cached per
process), validates them against `agent_config` at startup
(`validate_personas_against_config`), and resolves a persona's system prompt
at agent runtime (`resolve_persona_for_agent` — an agent with no persona
falls back to its class prompt). A chat agent knows it is speaking inside a
conversation when its payload carries `run_uuid`; it then builds the
conversation prompt (`build_conversation_prompt`: identity, turn number,
budget, the DONE contract, and the last `CONVO_LAST_N = 6` visible messages).

## The run row and its guards

`conversation_run` (see `data-model.md`) holds the bounded state:
`status` (`running` | `paused` | `finished` | `failed` | `stopped`), `turn`,
`tick_count`, `participants` (JSONB list with `turn_order`), `turn_policy`,
`budget`, the in-flight markers (`active_turn`, `active_speaker_uuid`,
`active_turn_enqueued_at`, `last_speaker_journal_id`), `retry_count`,
`stop_requested`, `reason`, and the `last_human_message_id` watermark.

Turn policy (JSONB): `mode` (`round_robin` — the only Phase 0 mode),
`min_turns` (floor before stop phrases apply, so a persona can't declare DONE
on turn 0), `max_turns` (default 12), `stop_phrases` (default
`["DONE", "NO_REPLY"]`), optional `max_wall_clock_seconds`. Budget holds
`wall_clock_started_at` (reset on resume, so idle time doesn't count).

Three compare-and-set guards keep the loop idempotent under double delivery
and restarts (`db/conversation.py`):

- **`claim_conversation_tick(run, expected_tick_count)`** — manual
  start/resume ticks CAS on a monotonic `tick_count`; a double-clicked button
  enqueues two ticks but only one claims.
- **`advance_conversation_if_new(run, src_journal_id, completed_turn)`** —
  routed speaker completions advance only when
  `turn == active_turn == completed_turn` and the journal id differs from
  `last_speaker_journal_id`: a redelivered completion is a no-op, and a stale
  completion from an earlier turn can't advance a later one. Success bumps
  `turn`, clears the in-flight markers, and resets `retry_count`.
- **`claim_failed_turn(...)`** — same WHERE clause, but bumps `retry_count`
  *without* advancing the turn, so the manager retries the same speaker. Past
  `MAX_TURN_RETRIES = 1` the run finishes `failed` (`reason="turn_failed"`).

## The turn cycle

The manager (`agents/conversation.py`, `ConversationManagerAgent`, role
`conversation`, fixed `CONVERSATION_MANAGER_UUID`) is an ordinary supervisor
agent — inbox in, journal out — that makes **no LLM calls**:

1. `POST /conversation/api/runs` creates the run and enqueues the first
   manager tick.
2. The manager evaluates stop conditions (`evaluate_stop`, in priority
   order: operator stop → `max_turns` → wall clock → stop phrase after the
   `min_turns` floor), checks for **human interruption** (a human message
   after the `last_human_message_id` watermark pauses the run), picks the
   next speaker (`next_speaker` — pure round-robin over `participants` by
   `turn_order`), marks the turn in flight, and enqueues the speaker with
   `{run_uuid, turn, room_uuid, persona_id, return_to_agent_uuid: <manager>}`.
3. The speaker posts its chat message like any chat agent and completes its
   journal. The supervisor's routing pass sees the **dynamic return address**
   (`result._routing.return_to_agent_uuid`) and enqueues the manager with the
   completion — failed speaker journals are routed back too (that's how
   retries work).
4. The manager CAS-advances (or CAS-claims the failure) and loops from 2.

Ends: `finished` (with `reason` ∈ `max_turns`, `wall_clock`, `stop_phrase`),
`stopped` (operator), `failed` (retries exhausted), or `paused` (human
interruption). `resume_conversation` works from `paused`/`stopped`/`failed`
(not `finished`): it clears stale in-flight markers, advances the human
watermark so the same interruption doesn't immediately re-pause, resets the
wall-clock anchor, and enqueues a fresh tick.

**Reconcile** (`reconcile_conversation`) recovers a run whose in-flight turn
went silent (a killed speaker whose completion never routed): if the turn has
been in flight longer than `STALE_TURN_TIMEOUT_SECONDS = 120`, it is treated
as failed (retry or fail the run); younger turns report `too_recent`.
Operator-triggered today (the Reconcile button); automatic/cron reconcile is
deferred.

## Operator surface

`/conversations` (`webapp/conversation_views.py`) shows a start panel
(template + room pickers) and a runs table (status pill, turn, reason, and
per-status actions: Stop / Resume / Reconcile / Open). The page polls
adaptively — 3s while a run is `running`/`paused`, 15s otherwise, nothing
while the tab is hidden. (The chat no-polling rules govern `/chat` itself;
this operator dashboard deliberately uses bounded polling instead of a second
SSE channel.)

API (`webapp/conversation_api.py`): `GET /conversation/api/templates`,
`GET/POST /conversation/api/runs`, `GET /conversation/api/runs/<id>`, and
`POST /conversation/api/runs/<id>/stop|resume|reconcile`.

> **Control-plane caveat.** Like the rest of the app, these endpoints are
> unauthenticated (security review Finding 8d) — starting/stopping
> conversations is localhost-trusted until the Phase 1 auth boundary lands.

## Design principles

- **The manager is mechanical.** Turn scheduling, bounds, and recovery are
  deterministic code; intelligence lives only in the personas. This keeps
  every scheduling decision testable without a model.
- **State is one row; the transcript is the room.** No parallel message
  store: a conversation reads like any chat room, and the run row can be
  reconstructed/inspected independently.
- **Every scheduling decision is CAS-guarded.** Double delivery, restarts,
  and stale completions are the normal case in a process-per-turn system,
  not an edge case.
- **Bounds are layered.** `max_turns`, wall clock, stop phrases (after a
  `min_turns` floor), operator stop, and human interruption each cut the
  loop independently.

Deferred (Phase 1+): automatic reconcile, a no-progress/loop detector,
LLM-selected speaker routing, the deliberation-protocol layer (issue ledger,
consensus, functional work personas), and token accounting. See
`proposals/2026-06-08-persona-prompts-and-agent-conversations.md` for the
original design intent.

## Reference

| Thing | Where |
|---|---|
| Manager + stop logic | `agents/conversation.py` (`ConversationManagerAgent`, `evaluate_stop`, `next_speaker`, `build_conversation_prompt`) |
| Run state + CAS guards | `db/conversation.py` (`create_conversation_run`, `claim_conversation_tick`, `advance_conversation_if_new`, `claim_failed_turn`, `resume_conversation`, `reconcile_conversation`) |
| Table | `conversation_run` (`db/models.py`; see `data-model.md`) |
| Personas + templates | `agents/persona.py`, `agent_profiles/` |
| Page + API | `webapp/conversation_views.py`, `webapp/conversation_api.py` |
| Constants | `DEFAULT_MAX_TURNS=12`, `DEFAULT_STOP_PHRASES=("DONE","NO_REPLY")`, `CONVO_LAST_N=6`, `MAX_TURN_RETRIES=1`, `STALE_TURN_TIMEOUT_SECONDS=120` |
| Tests | `agents/test_conversation.py` (pure logic + DB integration: CAS idempotency, full runs, bounds, pause/resume, retries, reconcile) |
