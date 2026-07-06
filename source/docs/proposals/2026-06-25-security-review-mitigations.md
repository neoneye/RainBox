# Rainbox security review and mitigation plan

**Purpose.** Brutal 80:20 review of the current Rainbox codebase, focused on
security flaws, serious anti-patterns, and incomplete features with real blast
radius. This is not a polish list. The findings below are limited to issues that
are significant, reproducible from the code, and worth fixing before expanding
the assistant/tool surface.

**Scope.** Static review of the Flask web app, admin surface, chat APIs,
assistant write approval flow, cron/backup, git inspection, startup migration,
the memory/Q&A control surface, the multimodal proxy, and the demo agent
substrate. The full test suite was not run for this review.

**Threat model, stated honestly.** The app is explicitly built as a *local
single-user demo*. `make_app()` even documents it: the session secret defaults
to a fixed `"rainbox-dev"` string, "This is a local single-user demo, so a
fixed dev default is fine; override with SECRET_KEY for anything exposed beyond
localhost" (`db/__init__.py:48-51`). `main.py` binds the server to
`127.0.0.1:5000`. Under that trust model — one operator, one machine, nothing
listening off-box — most of what follows is accepted risk, not a live exploit.

The reason this review still matters is that the *capability* behind that naked
boundary has grown faster than the boundary itself. Since this control plane was
first reviewed it has gained: encrypted database backup that can be redirected
to an attacker key, cron jobs that run shell commands and fire backups, file
edits and skill/memory activation through the assistant, a Q&A confidentiality
gate over personal data, and a multimodal proxy that spends the operator's model
API keys. Every one of those sits behind the same "localhost = operator" 
assumption, and localhost is reachable by local malware, browser extensions,
a stray SSH tunnel, a dev proxy, a forwarded port, or any future bind-address
change. This document takes the position that the boundary now needs to be a
real one, or the trust model needs to be enforced at the network/process layer
and documented as a hard constraint rather than a comment.

---

## Executive summary

Rainbox is a powerful local control plane: Flask-Admin, chat-triggered agents,
assistant write intents, cron jobs, encrypted database backup, git repo
inspection, memory, a Q&A knowledge base with per-entry shields, settings, model
provider probes, and a multimodal completion proxy. The internal agent
guardrails are good. The web boundary around them is still the prototype trust
model: **no authentication, no CSRF, and in several places client-supplied
operator identity, on every state-changing route.**

None of the findings from the earlier pass have been mitigated in code. The
surface has instead widened: new unauthenticated state-changing endpoints for
memory claims, Q&A shields, kanban, conversations, and the multimodal proxy all
inherit the same open boundary.

Fix order:

1. Add a real operator authentication boundary around every route, including
   Flask-Admin and all JSON APIs.
2. Add CSRF protection for browser-originated state changes, and stop signing
   sessions with the hardcoded `rainbox-dev` secret.
3. Stop accepting `sender_uuid` / operator identity from clients.
4. Lock down backup, cron, settings (including Q&A shields), memory-claim
   mutation, assistant write confirmation, and git filesystem inspection behind
   capability checks.
5. Replace destructive startup DDL with explicit migrations.

Findings 2–4 and 8 are separate exploit paths, but they share the root failure
of Finding 1: no authenticated operator boundary. Phase 1 collapses most of the
P0 surface; the later mitigations keep those surfaces safe even after
authentication exists.

---

## Finding 1: No auth; Flask-Admin is an unauthenticated database control panel

**Severity:** P0 · **Status:** open, unchanged

`webapp/core.py` creates Flask-Admin directly:

- `Admin(app, name="rainbox", url="/admin", theme=Bootstrap4Theme(fluid=True))`
  at `webapp/core.py:252-258`.
- No `is_accessible()` override on any view.
- No login route anywhere in the app.
- No global `before_request` guard (`db/__init__.py:make_app`, `main.py`).
- No CSRF layer in the webapp route surface. `WTForms` is present as a
  transitive dependency but Flask-WTF CSRF is not wired; admin view tests set
  `WTF_CSRF_ENABLED=False`, confirming it is not enforced.

Default writable `ModelView`s are registered for high-impact tables. Only a
handful are explicitly read-only (`AppSetting`, `SeedMemoryKb`,
`RetrievalEvent`, `MemoryEmbedding`); roughly thirty others allow
create/edit/delete by default, including:

- `Inbox`, `ModelConfig`, `ModelGroup`, `ModelGroupMember`, `AgentModelBinding`.
- `Chatroom`, `ChatMessage`, `ChatUser`, `ChatroomMember`, `ChatroomFolder`.
- `AssistantRun`, `AssistantStep`, `AssistantControl`, `AssistantWriteIntent`.
- `MemoryClaim`, `MemoryEvidence`, `MemoryRejectedValue`, `FeedbackEvent`.
- `CronFolder`, `CronJob`, `CronRun`; `GitFolder`, `GitRepo`.
- `KanbanBoardFolder`, `KanbanBoard`, `KanbanColumn`, `KanbanTask`.
- `WorkspaceShellState`, `Journal`, `EvalCase`, `EvalRun`, `EvalResult`,
  `ConversationRun`.

Flask-Admin's default `ModelView` is not a read-only viewer; it is a CRUD UI
unless restricted.

Binding to `127.0.0.1` reduces LAN exposure but does not make this safe.
Localhost is reachable by local malware, browser extensions, accidental SSH
tunnels, dev proxies, and any later deployment mistake.

### Impact

Anyone who can reach the local webserver can mutate the database directly:
create inbox work for agents; edit model config and agent bindings; alter
assistant write intents and controls; edit or delete chat/memory/eval/kanban
data; create or alter cron jobs; and read private chat content and operational
traces. This bypasses the careful code-level guardrails in the assistant — the
database becomes the capability boundary, and the database is exposed.

### Mitigations

1. Add authentication at the Flask app layer (`db/__init__.py:make_app`).
   - Minimal local option: random boot token printed to the terminal, stored in
     an HttpOnly SameSite cookie after login.
   - Better option: password configured through env only, with a signed session.
   - Do not store the password in `app_setting`.
2. Wrap Flask-Admin with a base admin view: subclass `ModelView`, implement
   `is_accessible()` and `inaccessible_callback()`, make read-only the default,
   require explicit opt-in for create/edit/delete.
3. Add a global route guard: require auth for all routes except
   health/static/login; protect JSON APIs and SSE as well as HTML pages.
4. Add regression tests: unauthenticated `/admin/` returns 401/302;
   unauthenticated `POST /chat/api/...`, cron, settings, memory, and
   assistant-write endpoints return 401/403.

---

## Finding 2: Chat message authorship is client-supplied and spoofable

**Severity:** P0 · **Status:** open, unchanged

`POST /chat/api/rooms/<room_uuid>/messages` (`webapp/chat_api.py:256-279`)
accepts `sender_uuid` from the JSON body. If present, the server only checks
that the sender is a member of the room; if absent, it falls back to the seeded
human user:

```python
sender_raw = data.get("sender_uuid")
if sender_raw:
    sender = _parse_uuid(sender_raw)
    if sender not in db.get_room_member_uuids(ruuid):
        abort(403, "sender is not a member of this room")
else:
    human = db.get_human_user()
    ...
    sender = human.uuid
```

Room and member discovery endpoints expose everything needed to spoof the
human: `GET /chat/api/rooms/<room_uuid>/members` (`chat_api.py:209-226`) returns
each member's `uuid`, `name`, and `user_type`, including the human operator.

`_maybe_trigger_chat_agents()` (`chat_api.py:60-91`) treats a message from a
`user_type == "human"` sender as an operator message and enqueues every
responder agent in the room. Since a caller can omit `sender_uuid` entirely and
default to the human, they do not even need to know a member UUID to trigger
agents.

### Impact

An unauthenticated caller can discover a room, post as the human operator, and
trigger the assistant, MCP agent, workspace shell agent, or query agents. This
is not just UI impersonation — it is execution authority over the agent queue.

### Mitigations

1. Delete `sender_uuid` from the public message-post API; the authenticated
   session determines the human sender.
2. Write agent messages only from server-side agent code, never from the browser
   API.
3. Split internal and external posting: browser endpoint is human-only,
   session-derived; internal helper is callable in Python by agent processes,
   not exposed as a generic HTTP impersonation endpoint.
4. If multi-user support is later needed, model real users and room ACLs — never
   identity as a writable request field.
5. Tests: posting with `sender_uuid` is ignored/rejected; a browser caller
   cannot post as an agent; agents cannot be triggered without an authenticated
   operator session.

---

## Finding 3: Settings, cron, and backup combine into a database-exfiltration path

**Severity:** P0 · **Status:** open, unchanged

The settings API changes registered settings via `POST /settings/api/set`
(`webapp/settings_views.py:358-377`), unauthenticated. The registry
(`db/settings.py:66-117`) includes high-impact backup settings — `backup.repo`,
`backup.age_recipient`, `backup.git_push` — alongside newer ones
(`cron.paused`, `assistant.disabled_capabilities`, `customize.dir`,
`qa.unlocked_shields`, `qa.facts_invalidated_at`). Only `backup.age_recipient`
has a custom validator (`_validate_age_recipient`, requires an `age1…` prefix);
the rest are coerced by type only. There is no audit log of who changed what.

The cron API (`webapp/cron_api.py`) allows replacing the whole cron tree
(`PUT /cron/api/tree`) and manually firing a job
(`POST /cron/api/jobs/<uuid>/run`), also unauthenticated. Cron action types are
`("message", "command", "backup", "memory_sync")` (`db/cron.py:38`). A `backup`
job resolves its destination from the job's `command` field or the `backup.repo`
setting, and recipients from `backup.age_recipient` (`db/cron.py:545-552`), then
calls `backup_database()` (`backup/dump.py:151-208`), which runs:

```text
pg_dump <dsn> | zstd | age -r <recipient>
```

The backup is encrypt-only and fails closed with no recipient — good for normal
operation. But an attacker who can set the recipient can encrypt the entire
database to a public key they control, with no approved-roots check on the
destination path (`dump.py:190-196`). If `backup.git_push` is enabled, the
resulting file is also committed and pushed to `origin`
(`db/cron.py:576-582`, `backup/remote.py`).

### Impact

An HTTP caller with local access can create/modify a backup job, set
`backup.age_recipient` to an attacker key, set the destination, and fire the job
— producing an encrypted dump of the Rainbox Postgres database readable only by
the attacker, optionally pushed to a remote.

### Mitigations

1. Put settings and cron behind authentication and CSRF first; without that, all
   finer controls are decorative.
2. Treat backup settings as sensitive: require re-auth or a high-risk
   confirmation token to change `backup.age_recipient`, `backup.repo`,
   `backup.git_push`; emit an audit event per change.
3. Restrict backup destinations to configured, approved roots; validate
   `backup.repo`; do not let a free-text cron `command` override the destination.
4. Separate recipient management from general settings. Prefer env-only
   recipients; if DB-stored, keep change history and require operator
   confirmation before the first backup with a new recipient.
5. Record the resolved repo, recipient fingerprint, trigger source, and
   requesting user before each backup run.

---

## Finding 4: Confirm-tier assistant writes are not operator-gated at the HTTP layer

**Severity:** P0 · **Status:** open, unchanged

The assistant's internal split is reasonable: confirm-tier writes are proposed,
not executed inline; `execute_write_intent()` runs the stored payload (with a
payload hash) only after confirmation. But the HTTP endpoints
(`webapp/chat_api.py:376-410`) do not authenticate the requester:

```python
# POST /chat/api/assistant/write-intents/<uuid>/confirm
human = db.get_human_user()
obs = execute_write_intent(intent_uuid, confirmed_by_uuid=human.uuid if human else None)
```

The reject and undo endpoints are the same shape. `confirmed_by_uuid` is filled
from the seeded demo user, so the audit trail records a human confirmation the
route never proved. There is no nonce or CSRF token — a bare POST with the intent
UUID in the path is enough.

Confirm-tier write kinds today include `EDIT_FILE`, `SET_REMINDER` (cron),
`ACTIVATE_MEMORY`, and `ACTIVATE_SKILL` (`agents/assistant.py`). Log-and-undo
writes (remember/forget memory, kanban create/move/complete/comment, and others)
execute without confirmation but are undoable.

### Impact

The confirm-tier safety story collapses at the route boundary. Any caller who
reaches the endpoint and obtains an intent UUID can approve file edits, reminder
scheduling, memory activation, and skill activation. Worse, it manufactures
false audit evidence: the database says the operator confirmed, but the route
did not authenticate anyone.

### Mitigations

1. Require an authenticated operator session for confirm/reject/undo.
2. Add CSRF tokens to the approval UI.
3. Bind write intents to room and user context; reject confirmation from the
   wrong session/user.
4. Consider one-time confirmation nonces: store a nonce with the proposed
   intent, render it only in the approval UI, require it on confirm/reject/undo.
5. Audit honestly: record requester identity and auth method; do not fill
   `confirmed_by_uuid` from `get_human_user()` unless the session proves that
   user.

---

## Finding 5: Startup performs destructive schema migration against live data

**Severity:** P1 · **Status:** partially mitigated (now guarded, still destructive on first legacy boot)

`db.init_db()` (`db/__init__.py:252-263`) still contains destructive DDL, but it
is now guarded so it runs at most once per legacy database. It drops the four
`assistant_*` tables only when the old integer `assistant_run.id` column exists:

```python
if _column_exists("assistant_run", "id"):
    db.session.execute(sa.text(
        "DROP TABLE IF EXISTS assistant_write_intent, assistant_control, "
        "assistant_step, assistant_run CASCADE"))
    db.session.commit()
db.create_all()
```

The guard is a genuine improvement over an unconditional drop: a steady-state
UUID-schema database no longer loses these tables on boot, and the surrounding
migration helpers (`_add_column_if_missing`, conditional constraint updates,
one-time backfills, the journal `id`→uuid conversion) are written to avoid
exclusive locks and preserve data. There is still no Alembic or formal migration
runner — migrations are ad-hoc idempotent helpers invoked from `init_db()`.

### Impact

The *first* boot of an older pre-UUID database silently drops
`assistant_write_intent`, `assistant_control`, `assistant_step`, and
`assistant_run` — traces, controls, approval state, pending write intents, and
undo history. It is one-time rather than every-boot, but that first run still
destroys operational audit history as a side effect of application startup, with
no backup gate. Schema migration remains coupled to app boot.

### Mitigations

1. Move destructive DDL out of `init_db()` into an explicit migration step.
2. Introduce a real migration runner (Alembic or project-local); make
   destructive migrations manual, loud, and backup-gated.
3. Where possible, preserve data: add UUID columns, backfill, repoint children,
   drop old columns only in a later verified migration.
4. If a table truly is disposable, require an explicit operator command, e.g.
   `rainbox migrate --drop-assistant-traces-after-backup`.
5. Test that startup never drops existing assistant write intents.

---

## Finding 6: Git page is arbitrary filesystem reconnaissance

**Severity:** P1 · **Status:** open, unchanged

`POST /git/api/check-path` (`webapp/git_api.py` → `db/git.py:238-254`) accepts an
arbitrary path, expands `~`, resolves it, and runs
`git -C <path> rev-parse --is-inside-work-tree`. There is no approved-roots
policy — any directory the server can read can be probed and registered.

`GET /git/api/repos/<uuid>/detail` (`db/git.py:257-293`) returns the absolute
path, existence, git-repo status, current branch, and a listing of the root
directory. Dotfiles are **not** filtered, so `.git`, `.env`, and similar appear
as raw entries.

The implementation uses `subprocess.run([...], shell=False)` with a timeout, so
this is not shell injection. The issue is authorization and path scope.

### Impact

A local filesystem oracle over HTTP: a caller can discover private repo paths and
root-level filenames (including sensitive dotfiles). Combined with the open
admin/API surface, the stored git tree becomes a map of local source checkouts.

### Mitigations

1. Require authentication for all git endpoints.
2. Add an approved-roots policy: only allow paths under configured workspace
   roots; reject `~` expansion unless it resolves under an approved root.
3. Store only normalized paths that pass policy.
4. Omit dotfiles by default in directory listings; surface `.git` existence as
   metadata, not a raw entry.
5. Tests: `/etc`, `$HOME`, and sibling repos outside approved roots are rejected;
   registered-repo detail refuses drift outside approved roots; unauthenticated
   git endpoints are denied.

---

## Finding 7: The headline demo pipeline is still a stub (now honestly documented)

**Severity:** P2 · **Status:** partially mitigated (docs now honest; behavior unchanged)

The default `ModelGroupAgent.handle()` (`agents/base.py:220-234`) intentionally
sleeps one second and returns the model group / candidate UUIDs without calling
an LLM. The `dreamer` / `critic` / `verifier` roles (`agents/config.py:77-80`)
have no specialized class in the dispatch table (`agents/__main__.py`), so they
fall through to this stub.

What has changed is honesty: the code comment now explains the stub is an
intentional default (it resolves and reports real model-group candidates so
binding can be verified without an LLM), and `README.md:394-410` states plainly
that the dreamer/critic/verifier pipeline still runs the `time.sleep(1)`
placeholder while the chat/tool/document agents make real provider-backed calls.
The product-honesty gap the earlier review flagged is largely closed at the
documentation level.

Meanwhile the real LLM-backed agents are numerous and genuine:
`StructuredChatAgent`, `UnstructuredChatAgent`, `FollowUpClassifierAgent`,
`RouterAgent`, `QueryRouterAgent`, `QueryFilterRouterAgent`, `ToolDemoAgent`,
`MCPAgent`, the document editors, and `AssistantAgent` (a full ReAct loop with
structured steps and real actions).

### Impact

Low. The supervisor, routing, process spawning, and journaling are real; only
the named demo roles are placeholders, and the README no longer oversells them.

### Mitigations

1. Either rename the demo as a supervisor/routing demo, or give each role a
   `StructuredLLMAgent` subclass with its own system prompt (the plumbing —
   model-group binding, fallback iteration, token counting, journaling — is
   already in place).
2. If kept as a stub, make the UI as explicit as the README ("no LLM call is
   made").
3. Add acceptance tests once wired: each role uses a system prompt, performs a
   structured model call through the bound model group, and journals failures.

---

## Finding 8: New surface added since this review widened the same open boundary

**Severity:** P1 (aggregate) · **Status:** new

The capability surface has grown; every new state-changing route inherits the
unauthenticated boundary of Finding 1. These are not separate trust bugs — they
are new blast radius behind the same missing lock.

### 8a. Q&A shields can be unlocked over HTTP (confidentiality gate)

The seed-memory Q&A system hides entries tagged with a `shield` unless that
shield is unlocked (`memory/seed_memory.py:_entry_hidden`), and the set of
unlocked shields is the `qa.unlocked_shields` setting. That setting is togglable
through the same unauthenticated `POST /settings/api/set`
(`settings_views.py:358-377`). Unlocking a shield exposes previously-hidden Q&A
answers — which the in-flight qa-overlay work is designed to hold *personal /
household data* — to the assistant on its next run. A confidentiality control
whose entire job is gating private data is flippable by any local caller with no
credential. Treat shield changes as a sensitive, authenticated, audited
operation.

### 8b. Memory-claim mutations are unauthenticated

`POST /memory/api/claims/<claim_uuid>/<action>` (`webapp/memory_api.py:185-259`)
allows `activate`, `reject`, `reactivate`, `correct`, `sensitivity`, and
`expiry` with no auth. A caller can activate an injected/false belief the
assistant will then trust, reject a true one, or flip a claim's sensitivity
(e.g. `secret` → `public`). `POST /api/memory/<uuid>/resolve` similarly resolves
conflict candidates. These should require the operator session and audit the
actor.

### 8c. Multimodal proxy spends the operator's model API keys

`POST /demo/multimodal/complete` (`multimodal_demo_views.py:546-605`) is
unauthenticated. **SSRF is not the issue** — the backend URL is resolved from a
ModelConfig / override via the provider registry (`_backend_base`), not from
caller input, so it cannot be pointed at an arbitrary host. The issue is that
the proxy forwards a ModelConfig-stored `api_key` as a bearer token
(`multimodal_demo_views.py:567-569`) to that backend. Any local caller can drive
the operator's configured (possibly paid, cloud) models on the operator's key,
at the operator's cost. Backend error bodies are also relayed verbatim — an
intentional demo choice, but a minor info-leak of upstream error text. Gate the
proxy behind auth; consider rate limits.

### 8d. Other unauthenticated state-changing routes

`POST /settings/api/repopulate_memory` rebuilds and re-embeds the Q&A KB (a
resource-heavy operation that can hammer Ollama); `POST /demo` calls
`reset_demo_data()` and re-enqueues demo tasks (destructive to demo state);
the kanban API (`POST /kanban/api/...`, ~12 routes) and conversation API
(`POST /conversation/api/runs`, stop/resume/reconcile) all mutate state with no
auth. These fold into the Phase 1 boundary rather than needing bespoke controls.

### Mitigations

Fold all of 8a–8d into the Phase 1 authentication boundary. Additionally: treat
`qa.unlocked_shields` and memory-claim `sensitivity` changes as high-sensitivity
(re-auth or confirmation token, audited); gate the multimodal proxy and add a
rate limit; audit memory-claim mutations with the real actor.

---

## Cross-cutting mitigation plan

### Phase 1: Put a lock on the control plane

- Add operator authentication (`db/__init__.py:make_app`).
- Replace the hardcoded `rainbox-dev` session secret with a required/random
  `SECRET_KEY`; set SameSite/HttpOnly/Secure cookie flags.
- Add CSRF for state-changing browser routes.
- Require auth on Flask-Admin, all JSON APIs, SSE that leaks room data, and all
  tool/proxy/demo pages.
- Make admin read-only by default.

Acceptance bar: unauthenticated state-changing requests fail; unauthenticated
admin pages fail; existing UI still works after login; tests cover chat, cron,
settings, memory, assistant write confirm, git, multimodal proxy, and admin.

### Phase 2: Remove client-controlled identity

- Remove `sender_uuid` from the public chat post; derive the human sender from
  the session.
- Prevent browser-originated agent messages.
- Record the real requester identity on write confirmations and controls.

Acceptance bar: a client cannot spoof the human or an agent; human posting still
triggers agents; write-intent audit records a real authenticated user.

### Phase 3: Harden high-risk capabilities

- Backup settings (`age_recipient`, `repo`, `git_push`) require elevated
  confirmation; destinations constrained to approved roots.
- Cron manual run requires auth + CSRF + audit.
- Git paths constrained to approved roots; dotfiles omitted by default.
- Q&A shield unlock and memory-claim sensitivity changes require elevated
  confirmation and audit.
- All settings changes are audited.

Acceptance bar: the database backup cannot be redirected by a generic API
caller; git cannot probe arbitrary filesystem locations; private Q&A data cannot
be unshielded without an authenticated, audited action; every high-risk mutation
has an audit row.

### Phase 4: Fix migration discipline

- Move destructive DDL out of `init_db()`; introduce an explicit migration
  runner; add backup-before-migrate guidance.

Acceptance bar: starting the app never drops existing assistant traces/intents;
schema migration is an explicit operator action.

### Phase 5: Make demos honest end-to-end

- Rename or implement the dreamer/critic/verifier stub; keep the UI as honest as
  the README already is.

Acceptance bar: "Run demo" does not imply LLM-backed agent reasoning unless it
actually does LLM-backed agent reasoning.

---

## Non-findings worth preserving

These are not praise items, but they affect mitigation priorities:

- The workspace shell avoids `shell=True`, uses an allowlist, and confines paths
  to a workspace root. The critical gap is who may trigger it, not shell
  injection.
- Confirm-tier write execution uses stored payloads and payload hashes. The
  critical gap is who may confirm, not the internal state machine.
- Backups are encrypt-only and fail closed without recipients. The critical gap
  is that the recipient/destination can be changed through an unauthenticated
  control plane.
- The multimodal proxy resolves its backend URL from the provider registry, not
  from caller input, so there is no SSRF. The gap is auth and API-key spend, not
  destination control.
- The session secret defaults to the fixed string `rainbox-dev`
  (`db/__init__.py:51`). Harmless while there are no sessions to forge, but it
  must become a required random secret the moment auth/sessions land — otherwise
  sessions are trivially forgeable.

---

## Bottom line

The agents have real safety boundaries; the web app around them is still a
prototype trust model, and the gap has widened since the first review. Rainbox
declares itself a local single-user demo, and under that assumption the open
control plane is accepted risk. But the capability behind the boundary now
includes database exfiltration, file edits, shell-running cron, personal-data
shields, and API-key spend — enough that the boundary should become real.
Authentication, request identity, CSRF, a non-default session secret, and
high-risk capability scoping should land before adding more assistant powers,
MCP adapters, or cron actions.
