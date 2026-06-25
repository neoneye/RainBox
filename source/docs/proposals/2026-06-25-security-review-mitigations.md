# Rainbox security review and mitigation plan (2026-06-25)

**Purpose.** Brutal 80:20 review of the current Rainbox codebase, focused on
security flaws, serious anti-patterns, and incomplete features with real blast
radius. This is not a polish list. The findings below are limited to issues that
are significant, reproducible from the code, and worth fixing before expanding
the assistant/tool surface.

**Scope.** Static review of the Flask web app, admin surface, chat APIs,
assistant write approval flow, cron/backup, git inspection, startup migration,
and the demo agent substrate. I did not run the full test suite for this review.

---

## Executive summary

Rainbox has grown into a powerful local control plane: Flask-Admin, chat-triggered
agents, assistant write intents, cron jobs, encrypted database backup, git repo
inspection, memory, settings, and model-provider probes. The internal agent
guardrails are better than the web boundary around them.

The dominant problem is that the app treats "bound to localhost" as equivalent
to "only the operator can use this." It is not. Any local process, browser
extension, malicious web page that can hit a reachable local endpoint, forwarded
port, reverse proxy mistake, or future bind-address change gets access to a
state-changing API with no login, no CSRF, no request identity, and in several
places client-supplied operator identity.

Fix order:

1. Add a real operator authentication boundary around every route, including
   Flask-Admin and JSON APIs.
2. Add CSRF protection for browser-originated state changes.
3. Stop accepting `sender_uuid` / operator identity from clients.
4. Lock down backup, cron, settings, assistant write confirmation, and git
   filesystem inspection behind capability checks.
5. Remove destructive startup migration from app boot.

---

## Finding 1: No auth; Flask-Admin is an unauthenticated database control panel

**Severity:** P0

`webapp/core.py` creates Flask-Admin directly:

- `Admin(app, name="rainbox", url="/admin", ...)` in `source/webapp/core.py`.
- No `is_accessible()` override.
- No login route.
- No global `before_request` guard.
- No CSRF layer was found in the webapp route surface.

Then default writable `ModelView`s are registered for high-impact tables:

- `Inbox`, `ModelConfig`, model groups, agent bindings.
- `Chatroom`, `ChatMessage`, `ChatUser`, memberships.
- `AssistantRun`, `AssistantStep`, `AssistantControl`,
  `AssistantWriteIntent`.
- `CronFolder`, `CronJob`, `CronRun`.
- Kanban, memory, feedback, eval, git tables.

Several views are explicitly read-only, but most are not. Flask-Admin's default
`ModelView` is not a read-only viewer; it is a CRUD UI unless restricted.

Binding the server to `127.0.0.1` in `main.py` reduces LAN exposure, but it does
not make this safe. Localhost is reachable by local malware, browser extensions,
accidental SSH tunnels, dev proxies, and any later deployment mistake.

### Impact

Anyone who can reach the local webserver can mutate the database directly:

- create inbox work for agents;
- edit model config and agent bindings;
- alter assistant write intents and controls;
- edit or delete chat/memory/eval/kanban data;
- create or alter cron jobs;
- inspect operational traces and private chat content.

This bypasses the careful code-level guardrails in the assistant. The database
becomes the capability boundary, and the database is exposed.

### Mitigations

Implement a mandatory operator session before adding more features:

1. Add authentication at the Flask app layer.
   - Minimal local option: random boot token printed to the terminal and stored
     in an HttpOnly SameSite cookie after login.
   - Better option: password configured through env only, with a signed session.
   - Do not store the password in `app_setting`.

2. Wrap Flask-Admin with a base admin view:
   - subclass `ModelView`;
   - implement `is_accessible()` and `inaccessible_callback()`;
   - make read-only the default;
   - require explicit opt-in for create/edit/delete.

3. Add a global route guard.
   - Require auth for all routes except health/static/login.
   - Protect JSON APIs as well as HTML pages.

4. Add regression tests:
   - unauthenticated `/admin/` returns 401/302;
   - unauthenticated `POST /chat/api/...` returns 401/403;
   - unauthenticated cron/settings/assistant-write endpoints return 401/403.

---

## Finding 2: Chat message authorship is client-supplied and spoofable

**Severity:** P0

`POST /chat/api/rooms/<room_uuid>/messages` accepts `sender_uuid` from the JSON
body. If present, the server only checks that the sender UUID is a member of the
room. If absent, it uses the seeded human user.

Room and member discovery endpoints expose the data needed to spoof the human:

- `GET /chat/api/rooms` lists room UUIDs.
- `GET /chat/api/rooms/<room_uuid>/members` returns each member UUID and
  `user_type`, including the human operator.

`_maybe_trigger_chat_agents()` then treats messages from a `user_type == "human"`
sender as operator messages and enqueues responder agents.

### Impact

An unauthenticated caller can:

- discover a room;
- discover the human operator UUID;
- post as that human;
- trigger the assistant, MCP agent, workspace shell agent, query agents, or other
  responders in the room.

This is not just impersonation in the UI. It is execution authority over the
agent queue.

### Mitigations

1. Delete `sender_uuid` from the public message-post API.
   - The authenticated session determines the human sender.
   - Agent messages should be written only by server-side agent code, not by the
     browser API.

2. If multi-user support is later needed, introduce real users and room ACLs.
   - Do not model identity as a writable request field.

3. Split internal and external posting APIs.
   - Browser endpoint: human-only, session-derived sender.
   - Internal helper: callable by agent processes in Python, not exposed as a
     generic HTTP impersonation endpoint.

4. Add tests:
   - posting with `sender_uuid` is ignored or rejected;
   - a browser caller cannot post as an agent;
   - a browser caller cannot trigger agents without an authenticated operator
     session.

---

## Finding 3: Settings, cron, and backup combine into a database-exfiltration path

**Severity:** P0

The settings API allows changing registered settings:

- `POST /settings/api/set`.

The registry includes high-impact backup settings:

- `backup.repo`;
- `backup.age_recipient`;
- `backup.git_push`.

The cron API allows replacing the cron tree and manually firing a job:

- `PUT /cron/api/tree`;
- `POST /cron/api/jobs/<job_uuid>/run`.

Backup cron jobs read the backup destination from the job command or
`backup.repo`, read recipients from `backup.age_recipient`, then call
`backup_database()`, which runs:

```text
pg_dump <dsn> | zstd | age -r <recipient>
```

The backup is encrypted, which is good for normal operation, but an attacker who
can set the public recipient can encrypt the database to themselves.

### Impact

An HTTP caller with access to the local app can create or modify a backup job,
set the age recipient to an attacker-controlled public key, set the backup
destination, and manually fire the job. That produces an encrypted dump of the
Rainbox Postgres database to an attacker-chosen local path. If git push is
enabled and the repo has credentials, it may also attempt remote upload.

### Mitigations

1. Put settings and cron behind authentication and CSRF first. Without that,
   all finer controls are decorative.

2. Treat backup settings as sensitive operations.
   - Require re-authentication or an explicit high-risk confirmation token for
     changing `backup.age_recipient`, `backup.repo`, and `backup.git_push`.
   - Show a clear audit event for every change.

3. Restrict backup destinations.
   - Allow only configured backup roots.
   - Validate that `backup.repo` is under an approved directory.
   - Do not let an arbitrary cron job override the destination with free text.

4. Separate backup recipient management from general settings.
   - Prefer env-only recipients for now.
   - If DB-stored recipients are required, keep a change history and require
     operator confirmation before first backup with a new recipient.

5. Add a backup dry-run/audit check.
   - Before running a backup, record the resolved repo, recipient fingerprint,
     trigger source, and requesting user.

---

## Finding 4: Confirm-tier assistant writes are not actually operator-gated at the HTTP layer

**Severity:** P0

The assistant implementation has a reasonable internal split:

- confirm-tier writes are proposed;
- the assistant does not execute them inline;
- `execute_write_intent()` executes the stored payload only after confirmation.

But the HTTP endpoint is:

```text
POST /chat/api/assistant/write-intents/<uuid>/confirm
```

It does not authenticate the requester. It simply looks up the seeded human user
and passes that UUID as `confirmed_by_uuid`.

The same issue applies to reject and undo endpoints. The audit trail records a
human confirmation even though the web layer did not prove a human made it.

### Impact

The confirm-tier safety story collapses at the route boundary. Any caller who
can reach the endpoint and knows or obtains an intent UUID can approve:

- file edits;
- reminder scheduling;
- memory activation;
- skill activation;
- future confirm-tier writes.

This is worse than a missing UI confirmation. It creates false audit evidence:
the database says the operator confirmed, but the route did not authenticate the
operator.

### Mitigations

1. Require authenticated operator session for confirm/reject/undo.

2. Add CSRF tokens to the approval UI.

3. Bind write intents to room and user context.
   - The confirming user must have access to the intent's room.
   - The endpoint should reject confirmation from the wrong session/user.

4. Consider one-time confirmation nonces.
   - Store a nonce with the proposed intent.
   - Render it only in the approval UI.
   - Require it on confirm/reject/undo.

5. Audit honestly.
   - Record requester identity, IP/UA if useful, and auth method.
   - Do not fill `confirmed_by_uuid` from `get_human_user()` unless the session
     proves that user.

---

## Finding 5: Startup performs destructive schema migration against live data

**Severity:** P1

`db.init_db()` checks whether `assistant_run.id` exists. If it does, startup
drops:

- `assistant_write_intent`;
- `assistant_control`;
- `assistant_step`;
- `assistant_run`.

The comment says assistant run history is disposable. That is not a safe
assumption anymore. These tables contain assistant traces, controls, approval
state, write intents, and undo history.

### Impact

Starting the app against an older database can silently delete operational audit
history and pending approvals. In the worst case, it destroys evidence needed to
understand or undo assistant writes.

This also mixes schema migration with application boot. Startup should not
perform irreversible data destruction as a side effect.

### Mitigations

1. Remove destructive DDL from `init_db()`.

2. Introduce explicit migrations.
   - Use Alembic or a project-local migration runner.
   - Make destructive migrations manual and loud.
   - Require a backup before running them.

3. Preserve data where possible.
   - Add new UUID columns.
   - Backfill.
   - Repoint children.
   - Drop old columns only in a later migration after verification.

4. If a table truly is disposable, require an explicit operator command:

```bash
rainbox migrate --drop-assistant-traces-after-backup
```

5. Add tests that startup does not drop existing assistant write intents.

---

## Finding 6: Git page is arbitrary filesystem reconnaissance

**Severity:** P1

`POST /git/api/check-path` accepts an arbitrary path, expands `~`, resolves it,
and runs `git -C <path> rev-parse --is-inside-work-tree`.

`GET /git/api/repos/<uuid>/detail` returns:

- absolute path;
- whether it exists;
- whether it is a git repo;
- current branch;
- root directory entries, including dotfiles and `.git`.

There is no approved-root policy. Any path the server process can read can be
probed, and any git repository can be registered in the Rainbox git tree.

### Impact

This is a local filesystem oracle over HTTP. A caller can discover private repo
paths and root-level filenames. Combined with the open admin/API surface, the
stored git tree becomes a map of local source checkouts.

The implementation uses `subprocess.run([...], shell=False)` and has a timeout,
so this is not shell injection. The issue is authorization and path scope.

### Mitigations

1. Require authentication for all git endpoints.

2. Add an approved roots policy.
   - Example: only allow paths under configured workspace roots.
   - Reject `~` expansion unless it resolves under an approved root.

3. Store only normalized paths that pass policy.

4. Redact or omit dotfiles by default in directory listings.
   - Show `.git` existence as metadata, not as a raw entry.

5. Add tests:
   - `/etc`, `$HOME`, and sibling repo paths outside approved roots are rejected;
   - registered repo detail refuses paths that drift outside approved roots;
   - unauthenticated git endpoints are denied.

---

## Finding 7: The headline demo pipeline is still a stub

**Severity:** P2

The default `ModelGroupAgent.handle()` intentionally sleeps for one second and
returns the model group/candidate UUIDs. It does not call an LLM. The README
also states that the `dreamer` / `critic` / `verifier` demo pipeline still uses
the placeholder.

This is not a security issue, but it is a product honesty issue. The app has real
chat/tool/document agents, but the named multi-agent demo pipeline is not doing
agent work.

### Impact

The architecture can look more complete than it is. A user can run the demo and
see durable routing, process spawning, and journaling, but not actual
dreamer/critic/verifier reasoning.

This matters because the project positions itself as an OS-process supervisor
for AI agents. The supervisor is real; the demo agents are not.

### Mitigations

1. Either rename the demo pipeline as a supervisor/routing demo, or wire it to
   real `StructuredLLMAgent` subclasses.

2. If kept as a stub, make the UI explicit:
   - "Run process supervisor demo";
   - show "no LLM call is made" in the result.

3. Add acceptance tests for the real path once implemented:
   - each role uses a system prompt;
   - each role performs a structured model call through the bound model group;
   - failures are journaled and routed according to the intended workflow.

---

## Cross-cutting mitigation plan

### Phase 1: Put a lock on the control plane

- Add operator authentication.
- Add CSRF for state-changing browser routes.
- Require auth on Flask-Admin, JSON APIs, SSE if it leaks private room data, and
  all tool/proxy pages.
- Make admin read-only by default.

Acceptance bar:

- unauthenticated state-changing requests fail;
- unauthenticated admin pages fail;
- existing UI still works after login;
- tests cover chat, cron, settings, assistant write confirm, git, and admin.

### Phase 2: Remove client-controlled identity

- Remove `sender_uuid` from public chat post.
- Derive human sender from session.
- Prevent browser-originated agent messages.
- Record requester identity on write confirmations and controls.

Acceptance bar:

- client cannot spoof the human or an agent;
- human message posting still triggers agents correctly;
- write-intent audit records a real authenticated user.

### Phase 3: Harden high-risk capabilities

- Backup settings require elevated confirmation.
- Backup destinations are constrained to approved roots.
- Cron manual run requires auth + CSRF + audit.
- Git paths are constrained to approved roots.
- Settings changes are audited.

Acceptance bar:

- database backup cannot be redirected by a generic API caller;
- git cannot probe arbitrary filesystem locations;
- every high-risk mutation has an audit row.

### Phase 4: Fix migration discipline

- Remove destructive startup migration.
- Introduce explicit migrations.
- Add backup-before-migrate guidance.

Acceptance bar:

- starting the app never drops existing assistant traces/intents;
- schema migration is an explicit operator action.

### Phase 5: Make demos honest

- Rename or implement the stub pipeline.
- Keep README and UI aligned with what actually runs.

Acceptance bar:

- "Run demo" does not imply LLM-backed agent reasoning unless it actually does
  LLM-backed agent reasoning.

---

## Non-findings worth preserving

These are not praise items, but they affect mitigation priorities:

- The workspace shell avoids `shell=True`, uses an allowlist, and confines paths
  to a workspace root. The critical gap is who may trigger it, not obvious shell
  injection.
- Confirm-tier write execution uses stored payloads and payload hashes. The
  critical gap is who may confirm, not the internal state machine.
- Backups are encrypt-only and fail closed without recipients. The critical gap
  is that the recipient/destination can be changed through an unauthenticated
  control plane.
- Model override creation does not expose raw `api_base` through the normal
  override form. The broader control-plane issue is still severe enough without
  overstating SSRF.

---

## Bottom line

The agents have started to grow real safety boundaries, but the web app around
them is still a prototype trust model. Right now, Rainbox is a powerful local
automation system with a naked HTTP control plane. Fixing authentication,
request identity, CSRF, and high-risk capability scoping should happen before
adding more assistant powers, MCP adapters, or cron actions.
